"""Acciones de gestión de cuentas y RBAC (fuente única del panel SSR y la API).

Cada función devuelve un **mensaje de error** (`str`) o `None` si quedó aplicado;
así el SSR lo muestra como flash y la API lo traduce a un error HTTP, sin duplicar
la lógica de permisos (ADR-0076/0077).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, update

from core.admin.auth import hash_password, revocar_sesiones
from core.admin.permissions import (
    ACCESS_NONE,
    OPT_IN_SECTIONS,
    OWNER_GRANTABLE_SECTIONS,
    OWNER_ONLY_SECTIONS,
    VALID_ACCESS,
    acceso_por_rol,
    catalog,
    pack_section_keys,
    seccion_padre,
)
from core.admin.roles import ROLES_DE_PLATAFORMA, es_tester
from core.db.models import Admin, AdminPermission, Tenant, TenantInvitation, TenantUser
from core.db.session import get_session
from core.mcp.types import PackProfile
from core.plugin_manager import plugin_manager
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

VALID_ROLES = ("master", "owner", "colaborador")


@dataclass(slots=True)
class AltaResultado:
    """Resultado de un alta: cuenta creada o invitación enviada (ADR-0081)."""

    estado: str  # "creado" | "invitado"


# ------------------------------------------------------------------ helpers
def scope_del_negocio(tenant_id: uuid.UUID | None) -> Any:
    """`tenant_scope` del negocio (para que la suscripción se lea con su RLS al medir el tope)
    o un contexto vacío si el alta no es para un negocio (una cuenta global del master)."""
    from contextlib import nullcontext

    from core.shared.tenant import tenant_scope

    return tenant_scope(tenant_id) if tenant_id is not None else nullcontext()


async def resolve_tenant_id(slug: str) -> uuid.UUID | None:
    async with get_session() as session:
        return (
            await session.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none()


async def ensure_tenant_link(admin_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
    """Vincula un admin a un tenant si no lo estaba (idempotente)."""
    async with get_session() as session:
        existing = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))


async def es_miembro_del_negocio(admin_id: uuid.UUID, tid: uuid.UUID | None) -> bool:
    """La cuenta está vinculada al negocio `tid` (cualquier rol). Es la comprobación mínima para
    LEER su estado desde ese negocio; las escrituras exigen además `target_allowed_for_owner`."""
    if tid is None:
        return False
    async with get_session() as session:
        vinculo = (
            await session.execute(
                select(TenantUser.admin_id).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tid
                )
            )
        ).scalar_one_or_none()
    return vinculo is not None


async def target_allowed_for_owner(
    admin_id: uuid.UUID, tid: uuid.UUID | None, *, actor: Admin | None = None
) -> bool:
    """Un owner solo gestiona cuentas EXCLUSIVAS de su tenant (no masters ni testers,
    ni cuentas con permisos globales o vinculadas a otros tenants) y, desde ADR-0136,
    solo las de rol `colaborador`: los propietarios son pares entre sí (un propietario
    invitado no toca al propietario real, ni al revés); a un owner lo gestiona el master.
    La regla es la misma para el tester (ADR-0134), que actúa como dueño del negocio
    activo: gestiona colaboradores, no propietarios. `actor` queda por compatibilidad."""
    if tid is None:
        return False
    async with get_session() as session:
        target = await session.get(Admin, admin_id)
        # Un tester (ADR-0134) es una cuenta de plataforma, como un master.
        if target is None or target.role in ROLES_DE_PLATAFORMA:
            return False
        link = (
            await session.execute(
                select(TenantUser.role).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tid
                )
            )
        ).scalar_one_or_none()
        if link is None:
            return False
        if link != "colaborador":
            return False
        global_perms = (
            await session.execute(
                select(func.count())
                .select_from(AdminPermission)
                .where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.tenant_id.is_(None),
                )
            )
        ).scalar_one()
        other_links = (
            await session.execute(
                select(func.count())
                .select_from(TenantUser)
                .where(TenantUser.admin_id == admin_id, TenantUser.tenant_id != tid)
            )
        ).scalar_one()
    return global_perms == 0 and other_links == 0


async def apply_pack_roles(
    values: list[str], tenant_id: uuid.UUID | None, admin_id: uuid.UUID | None
) -> None:
    """Avisa a los packs los roles de colaborador otorgados (ADR-0066).

    Un fallo del pack no tumba el alta de la cuenta: se loguea y se sigue.
    """
    if not values or tenant_id is None or admin_id is None:
        return
    modules = {m.id: m for m in plugin_manager.list_modules()}
    for value in values:
        pack_id, _, role_id = value.partition(":")
        module = modules.get(pack_id)
        if module is None or not role_id:
            continue
        try:
            await module.on_collaborator_role(tenant_id, admin_id, role_id)
        except Exception:
            log.warning("on_collaborator_role_failed", pack=pack_id, role=role_id, exc_info=True)


# ------------------------------------------------------------------ acciones
async def create_collaborator(
    *,
    actor: Admin,
    email: str,
    password: str,
    role: str,
    username: str = "",
    nombre: str = "",
    telefono: str = "",
    tenant_slug: str = "",
    pack_roles: list[str] | None = None,
) -> AltaResultado | str:
    is_master = actor.role == "master"
    email = email.strip().lower()
    if not email or not password:
        return "email y contraseña son obligatorios"
    if role not in VALID_ROLES:
        return "rol inválido"
    if role == "master" and not is_master:
        return "no podés crear un master de plataforma"
    if role == "owner" and not is_master and not es_tester(actor):
        # ADR-0048 §3 y ADR-0136: un propietario crea colaboradores; otro propietario lo
        # vincula el master.
        return "un propietario solo crea colaboradores; a otro propietario lo vincula el master"

    if is_master:
        admin_role = "master" if role == "master" else "colaborador"
        # Sin slug, la cuenta cae en el negocio activo (D-SPEC-34: el negocio ya no se elige).
        tenant_id = await resolve_tenant_id(tenant_slug) if tenant_slug else get_current_tenant_id()
        tenant_role = "colaborador" if role == "colaborador" else "owner"
    else:
        admin_role = "colaborador"
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return "no hay tenant activo"
        tenant_role = "owner" if role == "owner" else "colaborador"

    async with get_session() as session:
        existing = (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one_or_none()
        existing_id = existing.id if existing is not None else None

    if existing_id is not None:
        # Identidad ya registrada: no se duplica ni se toca su contraseña; se
        # invita al negocio y el usuario debe aceptar (ADR-0081).
        if tenant_id is None:
            return f"el admin {email} ya existe"
        from core.admin.invitations_service import crear_invitacion

        error = await crear_invitacion(
            tenant_id=tenant_id,
            admin_id=existing_id,
            invited_by=actor.id,
            role=tenant_role,
            pack_roles=pack_roles or [],
        )
        if error is not None:
            return error
        return AltaResultado(estado="invitado")

    async with scope_del_negocio(tenant_id), get_session() as session:
        if tenant_id is not None and tenant_role == "colaborador":
            # Tope de colaboradores del plan (D-35, ADR-0142): `QuotaExceeded` antes de crear
            # la cuenta; el caller lo traduce (409 `plan_limit_colaboradores`).
            from core.billing.enforcement import enforce_growth

            await enforce_growth(tenant_id, "colaboradores", session=session)
        usuario = username.strip() or None
        if usuario:
            ocupado = (
                await session.execute(select(Admin.id).where(Admin.username == usuario))
            ).scalar_one_or_none()
            if ocupado is not None:
                return "ese nombre de usuario ya está en uso"
        new_admin = Admin(
            email=email,
            username=usuario,
            nombre=nombre.strip() or None,
            telefono=telefono.strip() or None,
            password_hash=hash_password(password),
            role=admin_role,
        )
        session.add(new_admin)
        await session.flush()
        new_admin_id = new_admin.id
        if tenant_id is not None:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=new_admin.id, role=tenant_role))

    await apply_pack_roles(pack_roles or [], tenant_id, new_admin_id)
    return AltaResultado(estado="creado")


async def reset_password(*, actor: Admin, admin_id: uuid.UUID, password: str) -> str | None:
    if not password:
        return "la contraseña no puede estar vacía"
    if actor.role != "master" and not await target_allowed_for_owner(
        admin_id, get_current_tenant_id(), actor=actor
    ):
        return "no podés cambiar esa contraseña"
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None:
            return "admin no encontrado"
        admin.password_hash = hash_password(password)
        # Cambiar la contraseña desde Colaboradores cierra las sesiones abiertas de esa
        # cuenta (panel y app): quien tenía el token viejo vuelve al login.
        await revocar_sesiones(session, admin_id)
    return None


def _valid_sections() -> dict[str, str]:
    # Incluye las sub-secciones que aportan los packs (ADR-0063), p. ej.
    # `pack_calendar:notificaciones`, no solo el id del plugin.
    sections = set(plugin_manager.active_ids()) | pack_section_keys(plugin_manager.list_modules())
    return catalog(sections)


def secciones_del_catalogo() -> list[str]:
    """Claves de sección otorgables (core + packs) para la grilla de permisos."""
    return list(_valid_sections())


async def grant_permission(
    *,
    actor: Admin,
    admin_id: uuid.UUID,
    section: str,
    access: str,
    tenant_slug: str = "",
) -> str | None:
    is_master = actor.role == "master"
    if section not in _valid_sections():
        return "sección inválida"
    if access not in VALID_ACCESS:
        return "acceso inválido"

    if is_master:
        tenant_id = await resolve_tenant_id(tenant_slug) if tenant_slug else None
        if tenant_slug and tenant_id is None:
            return "tenant no encontrado"
    else:
        if section in OWNER_GRANTABLE_SECTIONS:
            return "no podés otorgar esa sección"
        if not await target_allowed_for_owner(admin_id, get_current_tenant_id(), actor=actor):
            return "no podés otorgar permisos a esa cuenta"
        tenant_id = get_current_tenant_id()
    if access == ACCESS_NONE and (tenant_id is None or section in OWNER_ONLY_SECTIONS):
        # Una denegación es por negocio (ADR-0136); Colaboradores no se deniega por fila.
        return "una denegación es por negocio"

    async with get_session() as session:
        if access == ACCESS_NONE and seccion_padre(section) is None:
            # Denegar un pack completo borra las filas de sus sub-secciones en ese negocio,
            # igual que `revoke_permission`: si no, un grant viejo de sub-sección seguiría
            # mandando sobre la denegación del pack (ADR-0136).
            await session.execute(
                delete(AdminPermission).where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.section.like(f"{section}:%"),
                    AdminPermission.tenant_id == tenant_id,
                )
            )
        row = (
            await session.execute(
                select(AdminPermission).where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.section == section,
                    AdminPermission.tenant_id.is_(None)
                    if tenant_id is None
                    else AdminPermission.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                AdminPermission(
                    admin_id=admin_id, section=section, access=access, tenant_id=tenant_id
                )
            )
        else:
            row.access = access
    return None


async def _hay_acceso_que_tapar(
    session: Any, admin_id: uuid.UUID, section: str, role: str | None, tenant_id: uuid.UUID | None
) -> bool:
    """¿Denegar en este alcance necesita una fila `none`? Sí si el rol hereda la sección (la misma
    regla que la resolución, ADR-0169 §1) o, en una sección opt-in, si una fila global (de un master)
    le da acceso en todos los negocios: el `none` del negocio la tapa (`resolver_explicito`)."""
    if section in OWNER_ONLY_SECTIONS:
        return False
    if acceso_por_rol(section, role, tenant_id) is not None:
        return True
    if tenant_id is None or section not in OPT_IN_SECTIONS:
        return False
    global_ = (
        await session.execute(
            select(AdminPermission.id).where(
                AdminPermission.admin_id == admin_id,
                AdminPermission.section == section,
                AdminPermission.tenant_id.is_(None),
                AdminPermission.access != ACCESS_NONE,
            )
        )
    ).first()
    return global_ is not None


async def revoke_permission(
    *,
    actor: Admin,
    admin_id: uuid.UUID,
    section: str,
    tenant_slug: str = "",
) -> str | None:
    """Quita el acceso a la sección (ADR-0136).

    Con alcance de negocio y un miembro cuyo rol heredaría la sección (`owner` → write,
    `colaborador` → read), «revocar» guarda una denegación (`none`): así la sección
    desaparece de verdad del menú, de Ctrl+K y de la API. Si no hay default que tapar
    (alcance global, sección de plataforma, cuenta sin vínculo), borra la fila como antes.
    Si la sección es un pack completo (`pack_cobranza`), borra también las filas de sus
    sub-secciones en ese alcance, para que la denegación del pack prevalezca.
    """
    is_master = actor.role == "master"
    if section not in _valid_sections():
        return "sección inválida"
    if is_master:
        tenant_id = await resolve_tenant_id(tenant_slug) if tenant_slug else None
        if tenant_slug and tenant_id is None:
            return "tenant no encontrado"
    else:
        if not await target_allowed_for_owner(admin_id, get_current_tenant_id(), actor=actor):
            return "no podés revocar permisos de esa cuenta"
        tenant_id = get_current_tenant_id()
    alcance = (
        AdminPermission.tenant_id.is_(None)
        if tenant_id is None
        else AdminPermission.tenant_id == tenant_id
    )
    async with get_session() as session:
        if seccion_padre(section) is None:
            await session.execute(
                delete(AdminPermission).where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.section.like(f"{section}:%"),
                    alcance,
                )
            )
        role = None
        if tenant_id is not None:
            role = (
                await session.execute(
                    select(TenantUser.role).where(
                        TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
        hereda = await _hay_acceso_que_tapar(session, admin_id, section, role, tenant_id)
        row = (
            await session.execute(
                select(AdminPermission).where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.section == section,
                    alcance,
                )
            )
        ).scalar_one_or_none()
        if not hereda:
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(
                AdminPermission(
                    admin_id=admin_id, section=section, access=ACCESS_NONE, tenant_id=tenant_id
                )
            )
        else:
            row.access = ACCESS_NONE
    return None


async def set_permissions(
    *,
    actor: Admin,
    admin_id: uuid.UUID,
    permissions: dict[str, str],
    tenant_slug: str = "",
) -> str | None:
    """Aplica en bloque los permisos de un colaborador (grilla, D-SPEC-34-1).

    Reemplaza las filas explícitas de `admin_permissions` del colaborador en el alcance
    (negocio o global): se borran todas y se reescriben desde `permissions`
    (`sección → none|read|write`). Una sección ausente del mapa vuelve al default del rol.
    `none` se guarda como fila solo cuando el rol heredaría la sección (ADR-0136); si no,
    la ausencia de fila ya la deniega. Reusa la validación de alcance de grant/revoke.
    """
    is_master = actor.role == "master"
    catalogo = _valid_sections()
    for section, access in permissions.items():
        if section not in catalogo:
            return f"sección inválida: {section}"
        if access not in VALID_ACCESS:
            return f"acceso inválido: {access}"

    if is_master:
        tenant_id = await resolve_tenant_id(tenant_slug) if tenant_slug else None
        if tenant_slug and tenant_id is None:
            return "tenant no encontrado"
    else:
        if not await target_allowed_for_owner(admin_id, get_current_tenant_id(), actor=actor):
            return "no podés cambiar los permisos de esa cuenta"
        for section in permissions:
            if section in OWNER_GRANTABLE_SECTIONS:
                return f"no podés otorgar {section}"
        tenant_id = get_current_tenant_id()

    alcance = (
        AdminPermission.tenant_id.is_(None)
        if tenant_id is None
        else AdminPermission.tenant_id == tenant_id
    )
    async with get_session() as session:
        role = None
        if tenant_id is not None:
            role = (
                await session.execute(
                    select(TenantUser.role).where(
                        TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()

        await session.execute(
            delete(AdminPermission).where(AdminPermission.admin_id == admin_id, alcance)
        )

        for section, access in permissions.items():
            if access == ACCESS_NONE:
                hereda = await _hay_acceso_que_tapar(session, admin_id, section, role, tenant_id)
                if not hereda:
                    # Sin fila ya queda denegado.
                    continue
            session.add(
                AdminPermission(
                    admin_id=admin_id, section=section, access=access, tenant_id=tenant_id
                )
            )
    return None


async def link_tenant(
    *, actor: Admin, admin_id: uuid.UUID, tenant_slug: str, role: str = "colaborador"
) -> str | None:
    """Vincular un admin existente a un tenant: solo master."""
    if actor.role != "master":
        return "solo un master puede vincular admins"
    if role not in ("owner", "colaborador"):
        return "rol inválido"
    tenant_id = await resolve_tenant_id(tenant_slug)
    if tenant_id is None:
        return "tenant no encontrado"
    async with get_session() as session:
        row = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))
        else:
            row.role = role
    return None


async def unlink_tenant(*, actor: Admin, admin_id: uuid.UUID, tenant_slug: str) -> str | None:
    """Desvincula una cuenta de un negocio (ADR-0136): borra su `tenant_user` y sus permisos
    de ese negocio, y revoca sus invitaciones pendientes a ese negocio (si no, aceptarlas
    deshacería el unlink). Master: cualquier vínculo. Owner o tester: solo del negocio activo
    y solo a un `colaborador` que no sea cuenta de plataforma, ni tenga permisos globales
    (esos los da y los quita el master), ni sea uno mismo (no exige exclusividad: sacar de
    MI negocio a una cuenta compartida no toca a los demás). Nadie deja un negocio sin
    propietario (las filas de owner se bloquean para que dos desvinculaciones simultáneas
    no lo vacíen). El perfil de gestor de pack-cobranza, si existía, queda sin vínculo y
    la app de campo deja de aceptarlo en el siguiente pedido."""
    is_master = actor.role == "master"
    if actor.id == admin_id:
        return "no podés desvincularte a vos mismo"
    tenant_id = await resolve_tenant_id(tenant_slug) if tenant_slug else None
    if tenant_id is None:
        return "tenant no encontrado"
    if not is_master and tenant_id != get_current_tenant_id():
        return "solo podés desvincular cuentas de tu negocio activo"
    async with get_session() as session:
        target = await session.get(Admin, admin_id)
        if target is None:
            return "admin no encontrado"
        link = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if link is None:
            return "la cuenta no está vinculada a ese negocio"
        if not is_master:
            if target.role in ROLES_DE_PLATAFORMA or link.role != "colaborador":
                return "no podés desvincular esa cuenta"
            permisos_globales = (
                await session.execute(
                    select(func.count())
                    .select_from(AdminPermission)
                    .where(
                        AdminPermission.admin_id == admin_id,
                        AdminPermission.tenant_id.is_(None),
                    )
                )
            ).scalar_one()
            if permisos_globales:
                return "esa cuenta tiene permisos globales: la gestiona el master"
        if link.role == "owner":
            owners = (
                await session.execute(
                    select(TenantUser.admin_id)
                    .where(TenantUser.tenant_id == tenant_id, TenantUser.role == "owner")
                    .with_for_update()
                )
            ).scalars().all()
            if not [o for o in owners if o != admin_id]:
                return "el negocio quedaría sin propietario: vinculá otro antes"
        await session.delete(link)
        await session.execute(
            update(TenantInvitation)
            .where(
                TenantInvitation.admin_id == admin_id,
                TenantInvitation.tenant_id == tenant_id,
                TenantInvitation.estado == "pendiente",
            )
            .values(estado="revocada", token_nonce=None, responded_at=func.now())
        )
        await session.execute(
            delete(AdminPermission).where(
                AdminPermission.admin_id == admin_id, AdminPermission.tenant_id == tenant_id
            )
        )
    return None


async def toggle_active(
    *, actor: Admin, admin_id: uuid.UUID, current_tenant_id: uuid.UUID | None = None
) -> str | None:
    is_master = actor.role == "master"
    if actor.id == admin_id:
        return "no podés desactivar tu propio usuario"
    if not is_master and not await target_allowed_for_owner(
        admin_id, current_tenant_id, actor=actor
    ):
        return "no podés modificar esa cuenta"
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None:
            return "admin no encontrado"
        reactivar = not admin.active
        negocios = (
            list(
                (
                    await session.execute(
                        select(TenantUser.tenant_id).where(
                            TenantUser.admin_id == admin_id, TenantUser.role == "colaborador"
                        )
                    )
                ).scalars()
            )
            if reactivar and admin.role not in ROLES_DE_PLATAFORMA
            else []
        )
    for tenant_id in negocios:
        # Reactivar vuelve a ocupar cupo en cada negocio donde la cuenta es colaboradora (una
        # desactivada no cuenta, ADR-0142): por encima del tope, `QuotaExceeded` y la cuenta
        # sigue desactivada. Conservador: también para el master (subir el plan es la salida).
        async with scope_del_negocio(tenant_id), get_session() as medida:
            from core.billing.enforcement import enforce_growth

            await enforce_growth(tenant_id, "colaboradores", session=medida)
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None:
            return "admin no encontrado"
        admin.active = not admin.active
        if not admin.active:
            # Desactivar invalida las sesiones en la misma transacción: el siguiente request
            # responde 401 y, si el master la reactiva, la cuenta vuelve a entrar por el login
            # (sin resucitar tokens viejos).
            await revocar_sesiones(session, admin_id)
    return None


def audit(action: str, actor: str, target: str, **detail: Any) -> None:
    """Rastro de cambios de cuentas/RBAC (el AuditLog de negocio exige tenant_id)."""
    log.info("admin_rbac_change", action=action, actor=actor, target=target, **detail)


async def pack_collaborator_roles() -> list[dict[str, str]]:
    """Roles de colaborador que aportan los packs ACTIVOS del tenant (ADR-0066)."""
    from core.agents.spec import CORE_PACK_ID, enabled_packs, load_active_spec

    enabled = enabled_packs(await load_active_spec())
    out: list[dict[str, str]] = []
    for module in plugin_manager.list_modules():
        if module.id not in enabled or module.id == CORE_PACK_ID:
            continue
        for role in getattr(module, "collaborator_roles", []) or []:
            role_id = str(role.get("id") or "").strip()
            if role_id:
                out.append({"value": f"{module.id}:{role_id}", "label": str(role.get("label") or role_id)})
    return out


# ------------------------------------------------------------------ perfiles de pack (ADR-0176)
PERFILES_TIMEOUT_S = 1.0


async def perfiles_de_pack(
    tenant_id: uuid.UUID,
    admin_ids: list[uuid.UUID],
    *,
    ver_detalle: Callable[[str], bool],
) -> dict[uuid.UUID, list[PackProfile]]:
    """Perfiles que los packs habilitados del negocio tienen de estos colaboradores (hook
    `collaborator_profiles`). Una llamada por pack con TODOS los ids (sin N+1), en paralelo y con un tope de
    `PERFILES_TIMEOUT_S` por pack: si uno falla o se pasa, el listado sale sin sus perfiles (`log.warning`).

    `ver_detalle(pack_id)` dice si quien pregunta ve el detalle de ese pack; si no, se conservan `pack`,
    `role_id`, `ref_id` y `etiqueta` y se descartan `resumen` y `edit`. Solo se aceptan ids pedidos."""
    if not admin_ids:
        return {}
    from core.agents.spec import enabled_packs, load_active_spec

    async with tenant_scope(tenant_id):
        habilitados = enabled_packs(await load_active_spec(tenant_id))
    modulos = [m for m in plugin_manager.list_modules() if m.id in habilitados]
    pedidos = set(admin_ids)

    async def pedir(modulo: Any) -> dict[uuid.UUID, list[PackProfile]]:
        try:
            async with tenant_scope(tenant_id):
                return await asyncio.wait_for(
                    modulo.collaborator_profiles(tenant_id, list(admin_ids)), PERFILES_TIMEOUT_S
                )
        except Exception:
            log.warning(
                "collaborator_profiles_fallo", pack=modulo.id, tenant_id=str(tenant_id), exc_info=True
            )
            return {}

    resultados = await asyncio.gather(*(pedir(m) for m in modulos))
    out: dict[uuid.UUID, list[PackProfile]] = {}
    for modulo, por_admin in zip(modulos, resultados, strict=True):
        detalle = ver_detalle(modulo.id)
        for admin_id, perfiles in por_admin.items():
            if admin_id not in pedidos:  # un pack no puede traer cuentas que no se pidieron
                continue
            for perfil in perfiles:
                if perfil.pack != modulo.id:
                    continue
                out.setdefault(admin_id, []).append(
                    perfil if detalle else perfil.model_copy(update={"resumen": None, "edit": None})
                )
    return out

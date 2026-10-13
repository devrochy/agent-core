"""Acciones de gestión de cuentas y RBAC (fuente única del panel SSR y la API).

Cada función devuelve un **mensaje de error** (`str`) o `None` si quedó aplicado;
así el SSR lo muestra como flash y la API lo traduce a un error HTTP, sin duplicar
la lógica de permisos (ADR-0076/0077).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select

from core.admin.auth import hash_password
from core.admin.permissions import OWNER_GRANTABLE_SECTIONS, catalog, pack_section_keys
from core.admin.roles import ROLES_DE_PLATAFORMA
from core.db.models import Admin, AdminPermission, Tenant, TenantUser
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

VALID_ROLES = ("master", "owner", "colaborador")
VALID_ACCESS = ("read", "write")


@dataclass(slots=True)
class AltaResultado:
    """Resultado de un alta: cuenta creada o invitación enviada (ADR-0081)."""

    estado: str  # "creado" | "invitado"


# ------------------------------------------------------------------ helpers
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


async def target_allowed_for_owner(admin_id: uuid.UUID, tid: uuid.UUID | None) -> bool:
    """Un owner solo gestiona cuentas EXCLUSIVAS de su tenant (no masters ni testers,
    ni cuentas con permisos globales o vinculadas a otros tenants)."""
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

    if is_master:
        admin_role = "master" if role == "master" else "colaborador"
        tenant_id = await resolve_tenant_id(tenant_slug) if tenant_slug else None
        if tenant_slug and tenant_id is None:
            return "tenant no encontrado"
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

    async with get_session() as session:
        new_admin = Admin(
            email=email,
            username=username.strip() or None,
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
        admin_id, get_current_tenant_id()
    ):
        return "no podés cambiar esa contraseña"
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None:
            return "admin no encontrado"
        admin.password_hash = hash_password(password)
    return None


def _valid_sections() -> dict[str, str]:
    # Incluye las sub-secciones que aportan los packs (ADR-0063), p. ej.
    # `pack_calendar:notificaciones`, no solo el id del plugin.
    sections = set(plugin_manager.active_ids()) | pack_section_keys(plugin_manager.list_modules())
    return catalog(sections)


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
        if not await target_allowed_for_owner(admin_id, get_current_tenant_id()):
            return "no podés otorgar permisos a esa cuenta"
        tenant_id = get_current_tenant_id()

    async with get_session() as session:
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


async def revoke_permission(
    *,
    actor: Admin,
    admin_id: uuid.UUID,
    section: str,
    tenant_slug: str = "",
) -> str | None:
    is_master = actor.role == "master"
    if section not in _valid_sections():
        return "sección inválida"
    if is_master:
        tenant_id = await resolve_tenant_id(tenant_slug) if tenant_slug else None
    else:
        if not await target_allowed_for_owner(admin_id, get_current_tenant_id()):
            return "no podés revocar permisos de esa cuenta"
        tenant_id = get_current_tenant_id()
    async with get_session() as session:
        await session.execute(
            delete(AdminPermission).where(
                AdminPermission.admin_id == admin_id,
                AdminPermission.section == section,
                AdminPermission.tenant_id.is_(None)
                if tenant_id is None
                else AdminPermission.tenant_id == tenant_id,
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


async def toggle_active(
    *, actor: Admin, admin_id: uuid.UUID, current_tenant_id: uuid.UUID | None = None
) -> str | None:
    is_master = actor.role == "master"
    if actor.id == admin_id:
        return "no podés desactivar tu propio usuario"
    if not is_master and not await target_allowed_for_owner(admin_id, current_tenant_id):
        return "no podés modificar esa cuenta"
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None:
            return "admin no encontrado"
        admin.active = not admin.active
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

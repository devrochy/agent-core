"""Invitaciones de membresía a un tenant (ADR-0081).

Fuente única para el panel (API `/api/v1/admin/invitations`) y la API de campo
(`/auth/invitaciones` del pack). El invitado es una identidad **existente**
(`admin_id`); los usuarios nuevos se vinculan directo (ver
`create_collaborator`/`create_collector`).

Enlace de un solo uso (ADR-0121): el aprovisionamiento automático invita al owner de un
negocio nuevo. Si la cuenta se creó sin contraseña (`SIN_CONTRASENA`), la invitación lleva
un enlace con el que el owner define su contraseña y entra; el token se deriva del id de
la invitación y de un nonce con `APP_SECRET_KEY`, y la base solo guarda el nonce.

Cada función devuelve un **mensaje de error** (`str`) o `None` si quedó aplicado.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update

from core.admin.collaborators_service import apply_pack_roles, ensure_tenant_link
from core.admin.roles import actua_como_master
from core.db.models import Admin, AdminPermission, Tenant, TenantInvitation, TenantUser
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

VALID_ROLES = ("owner", "colaborador")

# Hash de una cuenta creada sin contraseña (ADR-0121): no es un hash bcrypt, así que
# `verify_password` lo rechaza siempre y nadie puede entrar hasta que el owner la defina.
SIN_CONTRASENA = "!sin-contrasena:invitacion"
YA_PERTENECE = "el usuario ya pertenece a este negocio"
ENLACE_INVALIDO = "el enlace no es válido o ya venció"
INVITACION_VENCIDA = "la invitación venció: pídele al negocio una nueva"
# El enlace solo define la contraseña de una cuenta nueva y limpia (ADR-0121): si ya tiene
# contraseña, acceso a otro negocio u otra invitación pendiente, entra por la app.
CUENTA_EXISTENTE = (
    "la cuenta ya existe: entra con tu contraseña (o recupérala con «olvidé mi contraseña») "
    "y acepta la invitación en la app"
)
_CONTEXTO_TOKEN = "tenant-invitation:v1"
_MAX_TOKEN = 200


async def _invitado_valido(admin_id: uuid.UUID) -> str | None:
    """Guardas del invitado: existe, no es master ni tiene permisos globales."""
    async with get_session() as session:
        target = await session.get(Admin, admin_id)
        if target is None:
            return "usuario no encontrado"
        if target.role == "master":
            return "no se puede invitar a un master de plataforma"
        if target.role == "tester":
            # Cuenta de plataforma del centro de pruebas (ADR-0134): no entra a un negocio.
            return "no se puede invitar a un tester de plataforma"
        global_perms = (
            await session.execute(
                select(func.count())
                .select_from(AdminPermission)
                .where(AdminPermission.admin_id == admin_id, AdminPermission.tenant_id.is_(None))
            )
        ).scalar_one()
    if global_perms:
        return "la cuenta tiene permisos globales; no se puede invitar"
    return None


async def motivo_para_no_invitar(admin_id: uuid.UUID) -> str | None:
    """Por qué no se puede invitar a esa cuenta (master o permisos globales), o None."""
    return await _invitado_valido(admin_id)


async def crear_invitacion(
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    invited_by: uuid.UUID | None,
    role: str = "colaborador",
    pack_roles: list[str] | None = None,
) -> str | None:
    """Crea (o refresca) una invitación pendiente. Idempotente por (tenant, admin)."""
    if role not in VALID_ROLES:
        return "rol inválido"
    error = await _invitado_valido(admin_id)
    if error is not None:
        return error

    async with get_session() as session:
        link = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id
                )
            )
        ).scalar_one_or_none()
        if link is not None:
            return YA_PERTENECE
        pendiente = (
            await session.execute(
                select(TenantInvitation).where(
                    TenantInvitation.tenant_id == tenant_id,
                    TenantInvitation.admin_id == admin_id,
                    TenantInvitation.estado == "pendiente",
                )
            )
        ).scalar_one_or_none()
        if pendiente is not None:
            pendiente.role = role
            pendiente.pack_roles = list(pack_roles or [])
            return None
        session.add(
            TenantInvitation(
                tenant_id=tenant_id,
                admin_id=admin_id,
                invited_by=invited_by,
                role=role,
                pack_roles=list(pack_roles or []),
                estado="pendiente",
            )
        )
    log.info("tenant_invitation_created", tenant_id=str(tenant_id), admin_id=str(admin_id), role=role)
    return None


def _vigente():  # type: ignore[no-untyped-def]
    """Una invitación vencida (ADR-0121) ya no se muestra ni se puede aceptar."""
    return or_(TenantInvitation.expires_at.is_(None), TenantInvitation.expires_at > func.now())


async def pendientes_de(admin_id: uuid.UUID) -> list[dict[str, Any]]:
    """Invitaciones pendientes del admin (cross-tenant), con nombre/slug del negocio."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    TenantInvitation.id,
                    TenantInvitation.tenant_id,
                    Tenant.slug,
                    Tenant.name,
                    TenantInvitation.role,
                )
                .join(Tenant, Tenant.id == TenantInvitation.tenant_id)
                .where(
                    TenantInvitation.admin_id == admin_id,
                    TenantInvitation.estado == "pendiente",
                    _vigente(),
                    Tenant.deleted_at.is_(None),
                    Tenant.status == "active",
                )
                .order_by(Tenant.name)
            )
        ).all()
    return [
        {
            "id": str(iid),
            "tenantId": str(tid),
            "slug": slug,
            "nombre": nombre,
            "rol": role,
        }
        for iid, tid, slug, nombre, role in rows
    ]


async def contar_pendientes(admin_id: uuid.UUID) -> int:
    """Cuántas invitaciones le muestra `pendientes_de` (mismos filtros: vigentes y de negocios
    activos). El panel lo usa para avisarlas al entrar (HU-SH-06)."""
    async with get_session() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(TenantInvitation)
                .join(Tenant, Tenant.id == TenantInvitation.tenant_id)
                .where(
                    TenantInvitation.admin_id == admin_id,
                    TenantInvitation.estado == "pendiente",
                    _vigente(),
                    Tenant.deleted_at.is_(None),
                    Tenant.status == "active",
                )
            )
        ).scalar_one()


async def _invitacion_propia(session, invitation_id: uuid.UUID, admin_id: uuid.UUID) -> TenantInvitation | None:
    return (
        await session.execute(
            select(TenantInvitation).where(
                TenantInvitation.id == invitation_id,
                TenantInvitation.admin_id == admin_id,
                TenantInvitation.estado == "pendiente",
            )
        )
    ).scalar_one_or_none()


async def aceptar_invitacion(*, invitation_id: uuid.UUID, admin_id: uuid.UUID) -> str | None:
    from datetime import UTC, datetime

    async with get_session() as session:
        invitacion = await _invitacion_propia(session, invitation_id, admin_id)
        if invitacion is None:
            return "invitación no encontrada o ya resuelta"
        negocio = await session.get(Tenant, invitacion.tenant_id)
        if negocio is None or negocio.deleted_at is not None or negocio.status != "active":
            # Mismo filtro que `pendientes_de` (HU-SH-06): un negocio suspendido o borrado no
            # suma miembros por una invitación vieja.
            return "invitación no encontrada o ya resuelta"
        if invitacion.expires_at is not None and invitacion.expires_at <= datetime.now(UTC):
            # Una invitación vencida no da acceso aunque siga pendiente (ADR-0121).
            return INVITACION_VENCIDA
        invitacion.estado = "aceptada"
        invitacion.responded_at = datetime.now(UTC)
        invitacion.token_nonce = None  # el enlace (ADR-0121) deja de valer
        tenant_id = invitacion.tenant_id
        role = invitacion.role
        pack_roles = list(invitacion.pack_roles or [])
    await ensure_tenant_link(admin_id, tenant_id, role)
    await apply_pack_roles(pack_roles, tenant_id, admin_id)
    log.info("tenant_invitation_accepted", tenant_id=str(tenant_id), admin_id=str(admin_id))
    return None


async def rechazar_invitacion(*, invitation_id: uuid.UUID, admin_id: uuid.UUID) -> str | None:
    from datetime import UTC, datetime

    async with get_session() as session:
        invitacion = await _invitacion_propia(session, invitation_id, admin_id)
        if invitacion is None:
            return "invitación no encontrada o ya resuelta"
        invitacion.estado = "rechazada"
        invitacion.responded_at = datetime.now(UTC)
        invitacion.token_nonce = None
    log.info("tenant_invitation_rejected", invitation_id=str(invitation_id), admin_id=str(admin_id))
    return None


async def revocar_invitacion(
    *, invitation_id: uuid.UUID, tenant_id: uuid.UUID, actor: Admin
) -> str | None:
    """Revoca una invitación pendiente del tenant (owner del tenant o master)."""
    is_master = actor.role == "master"
    async with get_session() as session:
        invitacion = (
            await session.execute(
                select(TenantInvitation).where(
                    TenantInvitation.id == invitation_id,
                    TenantInvitation.estado == "pendiente",
                )
            )
        ).scalar_one_or_none()
        if invitacion is None:
            return "invitación no encontrada"
        if invitacion.tenant_id != tenant_id and not is_master:
            return "no podés revocar esa invitación"
        # Un tester habilitado actúa como dueño del negocio activo (ADR-0134).
        if not is_master and not actua_como_master(actor):
            link = (
                await session.execute(
                    select(TenantUser.role).where(
                        TenantUser.tenant_id == tenant_id, TenantUser.admin_id == actor.id
                    )
                )
            ).scalar_one_or_none()
            if link != "owner":
                return "solo el propietario puede revocar invitaciones"
        invitacion.estado = "revocada"
        invitacion.token_nonce = None
        from datetime import UTC, datetime

        invitacion.responded_at = datetime.now(UTC)
    return None


# ------------------------------------------------------------------ enlace de un solo uso (ADR-0121)
def _firma(invitation_id: uuid.UUID, nonce: str) -> str:
    from core.config import get_settings

    clave = get_settings().app_secret_key.encode()
    mensaje = f"{_CONTEXTO_TOKEN}:{invitation_id}:{nonce}".encode()
    digest = hmac.new(clave, mensaje, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def token_de_invitacion(invitation_id: uuid.UUID, nonce: str) -> str:
    """Token del enlace: `<id>.<HMAC(APP_SECRET_KEY, id + nonce)>`. Determinista, para que
    un reintento de la entrega reenvíe el mismo enlace sin guardar el token."""
    return f"{invitation_id}.{_firma(invitation_id, nonce)}"


def partes_del_token(token: str) -> tuple[uuid.UUID, str] | None:
    """(id de la invitación, firma) o None si el token no tiene la forma esperada."""
    crudo = (token or "").strip()
    if len(crudo) > _MAX_TOKEN:
        return None
    identificador, separador, firma = crudo.partition(".")
    if not separador or not firma:
        return None
    try:
        return uuid.UUID(identificador), firma
    except ValueError:
        return None


def url_del_panel() -> str:
    from core.config import get_settings

    settings = get_settings()
    return (settings.panel_base_url or settings.public_base_url).rstrip("/")


def enlace_de_invitacion(token: str) -> str:
    """El token va en el fragmento (`#`): el navegador no lo manda al servidor, así que no
    queda en los logs del proxy. La página del panel lo lee y llama a `accept-invitation`."""
    return f"{url_del_panel()}/invitacion#{token}"


async def crear_invitacion_owner(
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    con_enlace: bool,
    vigencia: timedelta,
) -> tuple[TenantInvitation | None, str | None]:
    """Invitación `owner` del aprovisionamiento automático (ADR-0121). Idempotente por
    (tenant, admin): un reintento reusa la pendiente y no cambia su nonce.

    `con_enlace` solo para una cuenta sin contraseña: el enlace la define. Devuelve
    (invitación, None), (None, `YA_PERTENECE`) si ya es miembro o (None, error) si el
    invitado no se puede invitar (master o permisos globales, ADR-0081)."""
    error = await _invitado_valido(admin_id)
    if error is not None:
        return None, error
    async with get_session() as session:
        link = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id
                )
            )
        ).scalar_one_or_none()
        if link is not None:
            return None, YA_PERTENECE
        pendiente = (
            await session.execute(
                select(TenantInvitation).where(
                    TenantInvitation.tenant_id == tenant_id,
                    TenantInvitation.admin_id == admin_id,
                    TenantInvitation.estado == "pendiente",
                )
            )
        ).scalar_one_or_none()
        if pendiente is None:
            pendiente = TenantInvitation(
                tenant_id=tenant_id,
                admin_id=admin_id,
                invited_by=None,  # la identidad de servicio no es un Admin
                role="owner",
                pack_roles=[],
                estado="pendiente",
            )
            session.add(pendiente)
        pendiente.role = "owner"
        if con_enlace and not pendiente.token_nonce:
            pendiente.token_nonce = secrets.token_urlsafe(16)
            pendiente.expires_at = datetime.now(UTC) + vigencia
        await session.flush()
    log.info(
        "tenant_invitation_owner",
        tenant_id=str(tenant_id),
        invitation_id=str(pendiente.id),
        con_enlace=bool(pendiente.token_nonce),
    )
    return pendiente, None


@dataclass(frozen=True)
class CanjeHecho:
    """El enlace se canjeó: la cuenta, el negocio al que entró y su sesión nueva (ADR-0124)."""

    admin_id: uuid.UUID
    tenant_id: uuid.UUID
    token_de_sesion: str


async def canjear_invitacion(token: str, new_password: str) -> tuple[str | None, CanjeHecho | None]:
    """Canjea el enlace de un solo uso (ADR-0121): define la contraseña de una cuenta que el
    aprovisionamiento creó sin ella, acepta la invitación (`TenantUser`) y abre la sesión del
    dueño (ADR-0124: «defino mi contraseña y entro a mi negocio»). Devuelve `(error, None)` o
    `(None, CanjeHecho)`.

    Solo vale para una cuenta **limpia**: sin contraseña, sin acceso a ningún negocio y sin
    otras invitaciones pendientes. Si no, `CUENTA_EXISTENTE`: entra con su contraseña (o la
    recupera por correo) y acepta en la app (ADR-0081). La contraseña se escribe con un
    UPDATE condicional sobre `SIN_CONTRASENA`: si un reset llegó antes, el canje no la pisa.
    La contraseña, la aceptación, la membresía y la sesión van en la misma transacción: o
    queda todo o nada.
    """
    from core.admin.auth import hash_password, nueva_sesion

    partes = partes_del_token(token)
    if partes is None:
        return ENLACE_INVALIDO, None
    if not new_password or len(new_password) < 8:
        return "la contraseña debe tener al menos 8 caracteres", None
    invitation_id, firma = partes
    ahora = datetime.now(UTC)
    async with get_session() as session:
        invitacion = await session.get(TenantInvitation, invitation_id)
        if invitacion is None or invitacion.estado != "pendiente" or not invitacion.token_nonce:
            return ENLACE_INVALIDO, None
        nonce = invitacion.token_nonce
        if not hmac.compare_digest(firma.encode(), _firma(invitacion.id, nonce).encode()):
            return ENLACE_INVALIDO, None
        if invitacion.expires_at is not None and invitacion.expires_at <= ahora:
            return ENLACE_INVALIDO, None
        admin = await session.get(Admin, invitacion.admin_id)
        if admin is None or not admin.active:
            return ENLACE_INVALIDO, None
        tenant_id = invitacion.tenant_id
        # El canje abre una sesión (ADR-0124): se repiten las guardas del invitado, por si la
        # cuenta pasó a master o ganó permisos globales después de invitarla.
        if await _invitado_valido(admin.id) is not None:
            return CUENTA_EXISTENTE, None
        if await _tiene_otro_acceso(session, admin.id, tenant_id):
            return CUENTA_EXISTENTE, None
        # La contraseña solo se escribe si sigue sin tener una (carrera con un reset).
        cambio = await session.execute(
            update(Admin)
            .where(Admin.id == admin.id, Admin.password_hash == SIN_CONTRASENA)
            .values(password_hash=hash_password(new_password))
        )
        if getattr(cambio, "rowcount", 0) != 1:
            await session.rollback()
            return CUENTA_EXISTENTE, None
        # Uso único atómico: el UPDATE condicional gana una sola vez aunque lleguen dos
        # canjes a la vez con el mismo enlace.
        aceptada = await session.execute(
            update(TenantInvitation)
            .where(
                TenantInvitation.id == invitacion.id,
                TenantInvitation.estado == "pendiente",
                TenantInvitation.token_nonce == nonce,
            )
            .values(estado="aceptada", responded_at=ahora, token_nonce=None)
        )
        if getattr(aceptada, "rowcount", 0) != 1:
            await session.rollback()  # deshace también la contraseña
            return ENLACE_INVALIDO, None
        role = invitacion.role
        pack_roles = list(invitacion.pack_roles or [])
        admin_id = admin.id
        email = admin.email
        session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))
        token_de_sesion = nueva_sesion(session, admin_id)
    await apply_pack_roles(pack_roles, tenant_id, admin_id)

    from core.shared.audit import record_audit
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        await record_audit(
            actor=f"admin:{email}",
            action="invitacion_aceptada",
            output_data={"invitation_id": str(invitation_id), "rol": role, "por_enlace": True},
        )
    log.info(
        "tenant_invitation_redeemed",
        tenant_id=str(tenant_id),
        invitation_id=str(invitation_id),
        admin_id=str(admin_id),
    )
    return None, CanjeHecho(admin_id=admin_id, tenant_id=tenant_id, token_de_sesion=token_de_sesion)


async def _tiene_otro_acceso(session, admin_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:  # type: ignore[no-untyped-def]
    """¿La cuenta ya entra a algún negocio, o tiene invitaciones pendientes en otro?"""
    miembro = (
        await session.execute(select(TenantUser.id).where(TenantUser.admin_id == admin_id).limit(1))
    ).first()
    if miembro is not None:
        return True
    otra = (
        await session.execute(
            select(TenantInvitation.id)
            .where(
                TenantInvitation.admin_id == admin_id,
                TenantInvitation.estado == "pendiente",
                TenantInvitation.tenant_id != tenant_id,
            )
            .limit(1)
        )
    ).first()
    return otra is not None

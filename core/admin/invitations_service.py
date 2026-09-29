"""Invitaciones de membresía a un tenant (ADR-0081).

Fuente única para el panel (API `/api/v1/admin/invitations`) y la API de campo
(`/auth/invitaciones` del pack). El invitado es una identidad **existente**
(`admin_id`); los usuarios nuevos se vinculan directo (ver
`create_collaborator`/`create_collector`).

Cada función devuelve un **mensaje de error** (`str`) o `None` si quedó aplicado.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from core.admin.collaborators_service import apply_pack_roles, ensure_tenant_link
from core.db.models import Admin, AdminPermission, Tenant, TenantInvitation, TenantUser
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

VALID_ROLES = ("owner", "colaborador")


async def _invitado_valido(admin_id: uuid.UUID) -> str | None:
    """Guardas del invitado: existe, no es master ni tiene permisos globales."""
    async with get_session() as session:
        target = await session.get(Admin, admin_id)
        if target is None:
            return "usuario no encontrado"
        if target.role == "master":
            return "no se puede invitar a un master de plataforma"
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
            return "el usuario ya pertenece a este negocio"
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
    async with get_session() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(TenantInvitation)
                .where(
                    TenantInvitation.admin_id == admin_id,
                    TenantInvitation.estado == "pendiente",
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
        invitacion.estado = "aceptada"
        invitacion.responded_at = datetime.now(UTC)
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
        if not is_master:
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
        from datetime import UTC, datetime

        invitacion.responded_at = datetime.now(UTC)
    return None

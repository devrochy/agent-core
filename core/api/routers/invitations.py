"""Invitaciones de membresía (ADR-0081): `GET/POST /api/v1/admin/invitations`.

El invitado es una identidad existente. El panel las crea (owner/master) y las
resuelve el propio invitado (`pending`, `accept`, `reject`). La API de campo del
pack expone los mismos casos para la APK.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select

from core.admin.collaborators_service import resolve_tenant_id
from core.admin.invitations_service import (
    aceptar_invitacion,
    contar_pendientes,
    crear_invitacion,
    pendientes_de,
    rechazar_invitacion,
    revocar_invitacion,
)
from core.admin.roles import es_tester
from core.api.deps import Principal, get_principal, require
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_error, plan_limit_responses
from core.billing.enforcement import QuotaExceeded
from core.db.models import Admin
from core.db.session import get_session

router = APIRouter(prefix="/admin/invitations", tags=["admin · invitaciones"])


class InvitationItem(BaseModel):
    id: str
    tenantId: str
    slug: str
    nombre: str
    rol: str


class PendingResponse(BaseModel):
    items: list[InvitationItem]
    total: int


class InvitationCreate(BaseModel):
    email: str = ""
    admin_id: uuid.UUID | None = None
    role: str = "colaborador"
    pack_roles: list[str] = []
    tenant: str = ""


def _fail(error: str | None, code: str) -> None:
    if error is not None:
        raise ApiError.bad_request(error, code)


async def _resolver_invitado(payload: InvitationCreate) -> uuid.UUID:
    if payload.admin_id is not None:
        return payload.admin_id
    email = payload.email.strip().lower()
    if not email:
        raise ApiError.bad_request("Email o admin_id son obligatorios.", "missing_identity")
    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one_or_none()
    if admin is None:
        raise ApiError.bad_request(
            "No existe una cuenta con ese correo; creala como usuario nuevo.",
            "identity_not_found",
        )
    return admin.id


async def _resolver_tenant(principal: Principal, payload: InvitationCreate) -> uuid.UUID:
    if principal.admin.role == "master" and payload.tenant:
        tenant_id = await resolve_tenant_id(payload.tenant)
        if tenant_id is None:
            raise ApiError.bad_request("Tenant no encontrado.", "tenant_not_found")
        return tenant_id
    if principal.tenant_id is None:
        raise ApiError.bad_request("No hay negocio activo.", "no_tenant")
    return principal.tenant_id


@router.get("/pending", response_model=PendingResponse)
async def list_pending(
    principal: Annotated[Principal, Depends(get_principal)],
) -> PendingResponse:
    items = [InvitationItem(**i) for i in await pendientes_de(principal.admin.id)]
    return PendingResponse(items=items, total=len(items))


@router.post("", status_code=204, responses=plan_limit_responses("colaboradores"))
async def create_invitation(
    payload: InvitationCreate,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    """Al tope de colaboradores del plan, 409 `plan_limit_colaboradores` (ADR-0142)."""
    if payload.role == "owner" and principal.admin.role != "master" and not es_tester(principal.admin):
        # ADR-0048 §3 y ADR-0136, como `create_collaborator`: un propietario invita
        # colaboradores; a otro propietario lo vincula el master. Sin esto, invitar «owners»
        # saltaba además el tope de colaboradores del plan (ADR-0142).
        raise ApiError.forbidden("Un propietario solo invita colaboradores; a otro propietario lo vincula el master.")
    tenant_id = await _resolver_tenant(principal, payload)
    admin_id = await _resolver_invitado(payload)
    try:
        error = await crear_invitacion(
            tenant_id=tenant_id,
            admin_id=admin_id,
            invited_by=principal.admin.id,
            role=payload.role,
            pack_roles=payload.pack_roles,
        )
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from None
    _fail(error, "invitation_error")
    return Response(status_code=204)


@router.post("/{invitation_id}/accept", status_code=204)
async def accept_invitation(
    invitation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    _fail(
        await aceptar_invitacion(invitation_id=invitation_id, admin_id=principal.admin.id),
        "invitation_error",
    )
    return Response(status_code=204)


@router.post("/{invitation_id}/reject", status_code=204)
async def reject_invitation(
    invitation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    _fail(
        await rechazar_invitacion(invitation_id=invitation_id, admin_id=principal.admin.id),
        "invitation_error",
    )
    return Response(status_code=204)


@router.post("/{invitation_id}/revoke", status_code=204)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    if principal.tenant_id is None:
        raise ApiError.bad_request("No hay negocio activo.", "no_tenant")
    _fail(
        await revocar_invitacion(
            invitation_id=invitation_id, tenant_id=principal.tenant_id, actor=principal.admin
        ),
        "invitation_error",
    )
    return Response(status_code=204)


@router.get("/count", response_model=int)
async def count_pending(
    principal: Annotated[Principal, Depends(get_principal)],
) -> int:
    return await contar_pendientes(principal.admin.id)

"""Identidad del request: `GET /api/v1/admin/me`.

Sirve al panel y a las apps para resolver sesión, tenant activo y permisos tras
el login, sin adivinar el contrato. La lista COMPLETA de tenants no viaja aquí
(un master puede tener cientos): el selector usa `/api/v1/admin/tenants`.

`invitaciones_pendientes` (HU-SH-06): cuántas invitaciones esperan respuesta, para que el
panel las muestre al entrar (una cuenta que ya existía recibe la invitación en la app,
ADR-0121). La lista sale de `/api/v1/admin/invitations/pending`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.admin.invitations_service import contar_pendientes
from core.admin.roles import testers_habilitados
from core.api.deps import Principal, get_principal
from core.api.schemas import TenantOption

router = APIRouter(prefix="/admin", tags=["admin · sesión"])


class MeResponse(BaseModel):
    admin_id: uuid.UUID
    email: str
    username: str | None
    role: str
    tenant: TenantOption | None
    tenant_count: int
    permissions: dict[str, str | None]
    invitaciones_pendientes: int
    qa_testers_habilitados: bool = False


@router.get("/me", response_model=MeResponse)
async def get_me(principal: Annotated[Principal, Depends(get_principal)]) -> MeResponse:
    admin = principal.admin
    tenant = next(
        (
            TenantOption(id=tid, name=name, role=role, active=True)
            for (tid, name, role) in principal.tenants
            if tid == principal.tenant_id
        ),
        None,
    )
    return MeResponse(
        admin_id=admin.id,
        email=admin.email,
        username=admin.username,
        role=admin.role,
        tenant=tenant,
        tenant_count=len(principal.tenants),
        permissions=principal.permissions,
        invitaciones_pendientes=await contar_pendientes(admin.id),
        # El panel muestra «Testers» y su aviso solo si el rol existe en este entorno (ADR-0134).
        qa_testers_habilitados=testers_habilitados(),
    )

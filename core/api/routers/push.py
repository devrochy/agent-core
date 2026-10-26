"""Registro de dispositivos de push de la app oficial (`/api/v1/me/push-token`).

Cualquier admin autenticado registra el token de SU dispositivo en el negocio
activo (sin gate de sección: no es un dato del panel, es del teléfono). Alta
idempotente (reactiva un token dado de baja; si el dispositivo cambia de cuenta, el token se
reasigna a la cuenta que lo registra, ADR-0168) y baja por soft delete (ADR-0099).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.db.models import PushToken
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/me", tags=["me · push"])


class PushTokenRequest(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    platform: Literal["android", "ios"] = "android"


async def _registrar(principal: Principal, token: str, platform: str) -> None:
    async with get_session() as session:
        existente = (
            await session.execute(
                select(PushToken).where(
                    PushToken.tenant_id == principal.tenant_id,
                    PushToken.token == token,
                )
            )
        ).scalar_one_or_none()
        reasignado = existente is not None and existente.admin_id != principal.admin.id
        if existente is not None:
            # El token de Expo identifica el dispositivo: quien lo registra ahora es quien
            # lo tiene en la mano (ADR-0168). Se reasigna en vez de rechazar con 409.
            existente.admin_id = principal.admin.id
            existente.platform = platform
            existente.deleted_at = None
        else:
            session.add(
                PushToken(
                    tenant_id=principal.tenant_id,
                    admin_id=principal.admin.id,
                    token=token,
                    platform=platform,
                )
            )
        await session.commit()
    if reasignado:
        log.info(
            "push_token_reasignado",
            tenant_id=str(principal.tenant_id),
            admin_id=str(principal.admin.id),
        )
    log.info(
        "push_token_registrado",
        tenant_id=str(principal.tenant_id),
        admin_id=str(principal.admin.id),
        platform=platform,
    )


@router.post("/push-token", status_code=204)
async def register_push_token(
    payload: PushTokenRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")
    await _registrar(principal, payload.token.strip(), payload.platform)
    return Response(status_code=204)


@router.delete("/push-token", status_code=204)
async def remove_push_token(
    payload: PushTokenRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    from datetime import UTC, datetime

    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")
    async with get_session() as session:
        existente = (
            await session.execute(
                select(PushToken).where(
                    PushToken.tenant_id == principal.tenant_id,
                    PushToken.admin_id == principal.admin.id,
                    PushToken.token == payload.token.strip(),
                    PushToken.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existente is not None:
            existente.deleted_at = datetime.now(UTC)
            await session.commit()
    return Response(status_code=204)

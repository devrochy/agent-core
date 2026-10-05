"""Auditoría de acciones críticas de la app (`POST /api/v1/audit/mobile-events`).

La app encola sus acciones críticas (pagos, borrados, liquidaciones, cambios de
plan) y las reenvía con `eventId` idempotente por negocio (ADR-0097). La
respuesta da un ack por evento (`sincronizado`/`duplicado`/`error`), igual
criterio que el sync offline de cobranza.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.db.models import MobileAuditEvent
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["auditoría"])


class MobileEvent(BaseModel):
    eventId: uuid.UUID
    accion: str = Field(min_length=2, max_length=80)
    entidad: str = Field(min_length=2, max_length=80)
    entidadId: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    deviceId: str | None = Field(default=None, max_length=200)
    ocurridoEn: datetime | None = None


class MobileEventsRequest(BaseModel):
    events: list[MobileEvent] = Field(min_length=1, max_length=100)


class MobileEventAck(BaseModel):
    eventId: uuid.UUID
    estado: str
    error: str | None = None


class MobileEventsResponse(BaseModel):
    eventos: list[MobileEventAck]


@router.post("/audit/mobile-events", response_model=MobileEventsResponse)
async def registrar_eventos_moviles(
    payload: MobileEventsRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> MobileEventsResponse:
    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")

    acks: list[MobileEventAck] = []
    async with get_session() as session:
        for ev in payload.events:
            existe = (
                await session.execute(
                    select(MobileAuditEvent.id).where(
                        MobileAuditEvent.tenant_id == principal.tenant_id,
                        MobileAuditEvent.event_id == ev.eventId,
                        MobileAuditEvent.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existe is not None:
                acks.append(MobileEventAck(eventId=ev.eventId, estado="duplicado"))
                continue
            try:
                session.add(
                    MobileAuditEvent(
                        tenant_id=principal.tenant_id,
                        admin_id=principal.admin.id,
                        event_id=ev.eventId,
                        accion=ev.accion,
                        entidad=ev.entidad,
                        entidad_id=ev.entidadId,
                        payload=ev.payload,
                        device_id=ev.deviceId,
                        ocurrido_en=ev.ocurridoEn,
                    )
                )
                acks.append(MobileEventAck(eventId=ev.eventId, estado="sincronizado"))
            except Exception as exc:
                log.warning("mobile_audit_error", event_id=str(ev.eventId), exc_info=True)
                acks.append(
                    MobileEventAck(eventId=ev.eventId, estado="error", error=type(exc).__name__)
                )
        await session.commit()

    log.info(
        "mobile_audit_sync",
        tenant_id=str(principal.tenant_id),
        total=len(payload.events),
        nuevos=sum(1 for a in acks if a.estado == "sincronizado"),
    )
    return MobileEventsResponse(eventos=acks)

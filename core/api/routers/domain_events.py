"""Bandeja de eventos de dominio fallidos (outbox, ADR-0107 / HU-SUB-03).

Lista los `domain_events` en `fallido` del tenant activo y permite
re-encolarlos (vuelven a `pendiente` con reintento inmediato).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select, update

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.models import DomainEvent, utcnow
from core.db.session import get_session

router = APIRouter(prefix="/admin/domain-events", tags=["admin · domain events"])


class DomainEventEntry(BaseModel):
    id: str
    topic: str
    attempts: int
    last_error: str | None
    created_at: str


class DomainEventsPage(BaseModel):
    entries: list[DomainEventEntry]


@router.get("", response_model=DomainEventsPage)
async def list_failed(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> DomainEventsPage:
    """Últimos 50 eventos fallidos del tenant activo (RLS los acota)."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(DomainEvent)
                .where(
                    DomainEvent.tenant_id == principal.tenant_id,
                    DomainEvent.status == "fallido",
                )
                .order_by(DomainEvent.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
    return DomainEventsPage(
        entries=[
            DomainEventEntry(
                id=str(e.id),
                topic=e.topic,
                attempts=e.attempts,
                last_error=e.last_error,
                created_at=e.created_at.isoformat(),
            )
            for e in rows
        ]
    )


@router.post("/{event_id}/retry", status_code=204)
async def retry_event(
    event_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> Response:
    """Re-encola un evento fallido: vuelve a `pendiente` con reintento inmediato."""
    async with get_session() as session:
        row = await session.get(DomainEvent, event_id)
        if row is None or row.tenant_id != principal.tenant_id:
            raise ApiError.not_found("El evento no existe o no es de este negocio.")
        if row.status != "fallido":
            raise ApiError.bad_request("Solo se reintentan eventos fallidos.")
        await session.execute(
            update(DomainEvent)
            .where(DomainEvent.id == event_id)
            .values(status="pendiente", attempts=0, next_attempt_at=utcnow(), last_error=None)
        )
    return Response(status_code=204)

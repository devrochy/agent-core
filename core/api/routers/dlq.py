"""Bandeja de fallos (DLQ) del stream de entrada, migrada del panel clásico.

Lista los mensajes que fallaron tras N reintentos (stream Redis
`agent_core:inbound:dlq`) del tenant activo y permite re-encolarlos: se revoca
el claim de idempotencia (soft-delete de `ProcessedEvent`) y el evento vuelve al
stream de entrada (ADR-0013).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from core.api.deps import Principal, require
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/dlq", tags=["admin · dlq"])

DLQ_STREAM = "agent_core:inbound:dlq"
INBOUND_STREAM = "agent_core:inbound"


class DlqEntry(BaseModel):
    entry_id: str
    external_message_id: str
    tenant_id: str
    content: str


class DlqPage(BaseModel):
    entries: list[DlqEntry]


async def _entrada(entry_id: str) -> tuple[str, dict[str, Any]] | None:
    from core.memory.working import get_redis

    r = get_redis()
    rows = await r.xrange(DLQ_STREAM, min=entry_id, max=entry_id)
    if not rows:
        return None
    eid, fields = rows[0]
    return eid, dict(fields)


@router.get("", response_model=DlqPage)
async def list_dlq(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> DlqPage:
    """Últimos 200 eventos fallidos, solo del tenant activo."""
    from core.memory.working import get_redis

    r = get_redis()
    tenant_id = str(principal.tenant_id)
    entries = await r.xrange(DLQ_STREAM, count=200)
    return DlqPage(
        entries=[
            DlqEntry(
                entry_id=eid,
                external_message_id=str(fields.get("external_message_id", "")),
                tenant_id=str(fields.get("tenant_id", "")),
                content=str(fields.get("content", ""))[:200],
            )
            for eid, fields in entries
            if str(dict(fields).get("tenant_id", "")) == tenant_id
        ]
    )


@router.post("/{entry_id}/retry", status_code=204)
async def retry_dlq(
    entry_id: str,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> Response:
    """Re-encola un evento: revoca el claim de idempotencia y lo devuelve al stream."""
    from sqlalchemy import update

    from core.db.models import ProcessedEvent, utcnow
    from core.db.session import get_session
    from core.memory.working import get_redis
    from core.shared.tenant import tenant_scope

    encontrado = await _entrada(entry_id)
    if encontrado is None:
        raise ApiError.not_found("El evento ya no está en la bandeja.")
    _eid, fields = encontrado
    if str(fields.get("tenant_id", "")) != str(principal.tenant_id):
        raise ApiError.forbidden("No puedes reintentar eventos de otro negocio.")

    r = get_redis()
    async with tenant_scope(principal.tenant_id):
        async with get_session() as session:
            await session.execute(
                update(ProcessedEvent)
                .where(
                    ProcessedEvent.tenant_id == principal.tenant_id,
                    ProcessedEvent.external_message_id == fields.get("external_message_id", ""),
                    ProcessedEvent.deleted_at.is_(None),
                )
                .values(deleted_at=utcnow())
            )
    await r.xadd(INBOUND_STREAM, fields)
    await r.xdel(DLQ_STREAM, entry_id)
    return Response(status_code=204)

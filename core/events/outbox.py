"""Outbox de eventos de dominio (HU-SUB-03, ADR-0107).

`emit_domain` escribe el evento en la MISMA transacción que el cambio de
estado: usa la sesión que le pasás y NO commitea (el commit es del caller).
La inserción es idempotente por `(tenant_id, dedupe_key)`; la entrega es
at-least-once, así que los handlers deben ser idempotentes por su cuenta.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import DomainEvent, utcnow
from core.shared.logger import get_logger

log = get_logger(__name__)

# Claves de envoltura del outbox que NO se pasan a los handlers (van en el
# payload para trazabilidad, pero los handlers conservan su firma original).
ENVELOPE_KEYS = ("tenant_id", "occurred_at")


async def emit_domain(
    session,
    *,
    tenant_id: uuid.UUID,
    topic: str,
    dedupe_key: str,
    payload: dict[str, Any],
    entregar_desde: datetime | None = None,
) -> bool:
    """INSERT idempotente del evento en la sesión activa. True si se insertó.

    `entregar_desde` difiere la entrega (el despachador solo toma eventos con
    `next_attempt_at <= ahora`): p. ej. un aviso de cobro que espera el horario legal
    (HU-SUB-18). Por defecto, ya.

    El payload queda con `tenant_id` y `occurred_at` explícitos (contrato del
    tenant en el payload, Gherkin HU-SUB-03)."""
    data = {**payload, "tenant_id": str(tenant_id), "occurred_at": utcnow().isoformat()}
    stmt = (
        pg_insert(DomainEvent)
        .values(
            tenant_id=tenant_id,
            topic=topic,
            payload=data,
            status="pendiente",
            attempts=0,
            next_attempt_at=entregar_desde or utcnow(),
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
    )
    result = await session.execute(stmt)
    insertado = bool(getattr(result, "rowcount", 0))
    if not insertado:
        log.info("domain_event_duplicado", topic=topic, dedupe_key=dedupe_key)
    return insertado


def payload_para_handlers(payload: dict[str, Any]) -> dict[str, Any]:
    """Payload sin las claves de envoltura, con la firma que esperan los handlers."""
    return {k: v for k, v in payload.items() if k not in ENVELOPE_KEYS}

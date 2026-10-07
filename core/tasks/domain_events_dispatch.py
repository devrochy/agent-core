"""Despachador del outbox de dominio (HU-SUB-03, ADR-0107).

El beat (30 s) toma los `domain_events` pendientes vencidos (enumeración
SECURITY DEFINER), reclama cada uno en Redis y lo entrega bajo el
`tenant_scope` de su tenant re-emitiendo por el bus in-process con
`emit_strict`: los handlers existentes no cambian de firma y un fallo real
reintenta con backoff. Éxito → `entregado`; al agotar 5 intentos → `fallido`
con `last_error` (visible en la Bandeja de fallos y reintentable a mano).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from core.db.admin_enum import admin_domain_events_due
from core.db.models import DomainEvent, utcnow
from core.db.session import get_session
from core.events.outbox import payload_para_handlers
from core.shared.async_bridge import run_async
from core.shared.events import event_bus
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

BATCH = 200
MAX_ATTEMPTS = 5
BACKOFF_S = (30, 60, 120, 300, 600)
_CLAIM_PREFIX = "domain_event:claim:"
_CLAIM_TTL_S = 600


def backoff_seconds(attempt: int) -> int:
    """Espera exponencial para el intento `attempt` (0-based)."""
    return BACKOFF_S[min(max(attempt, 0), len(BACKOFF_S) - 1)]


async def _claim(event_id: uuid.UUID) -> bool:
    """Claim atómico (Redis SETNX): una sola entrega viva por evento."""
    from core.memory.working import get_redis

    try:
        claimed = await get_redis().set(
            f"{_CLAIM_PREFIX}{event_id}", "1", nx=True, ex=_CLAIM_TTL_S
        )
        return bool(claimed)
    except Exception:
        return True


async def _release(event_id: uuid.UUID) -> None:
    from core.memory.working import get_redis

    try:
        await get_redis().delete(f"{_CLAIM_PREFIX}{event_id}")
    except Exception:
        pass


async def dispatch_due_events(limit: int = BATCH) -> int:
    """Entrega los eventos pendientes vencidos. Devuelve cuántos procesó."""
    procesados = 0
    ahora = utcnow()
    for event_id, tenant_id in await admin_domain_events_due(limit, ahora):
        if not await _claim(event_id):
            continue
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    evento = await session.get(DomainEvent, event_id)
                    if evento is None or evento.status != "pendiente":
                        continue
                    try:
                        await event_bus.emit_strict(
                            evento.topic, **payload_para_handlers(evento.payload)
                        )
                        evento.status = "entregado"
                        evento.last_error = None
                    except Exception as exc:
                        evento.attempts += 1
                        evento.last_error = str(exc)[:2000]
                        if evento.attempts >= MAX_ATTEMPTS:
                            evento.status = "fallido"
                            log.error(
                                "domain_event_failed",
                                event_id=str(event_id),
                                tenant_id=str(tenant_id),
                                topic=evento.topic,
                                attempts=evento.attempts,
                            )
                        else:
                            evento.next_attempt_at = ahora + timedelta(
                                seconds=backoff_seconds(evento.attempts - 1)
                            )
            procesados += 1
        finally:
            await _release(event_id)
    return procesados


async def _run_dispatch() -> None:
    procesados = await dispatch_due_events()
    if procesados:
        log.info("domain_events_dispatched", total=procesados)


def dispatch_domain_events_task() -> None:
    """Entrada Celery del despachador (registrada en celery_app)."""
    run_async(_run_dispatch())

"""HU-ARCH-06: reclamo de pendientes (XAUTOCLAIM), claim con estado y DLQ alcanzable.

Incidente 2026-10-04: varios mensajes quedaron `pending` para siempre (el
`xreadgroup '>'` solo lee mensajes nuevos, no había reclamo) y la DLQ nunca se
alcanzaba. Requiere el stack local (Redis + Postgres).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from conftest import PHONE, TEST_ALPHA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _fields(message_id: str, content: str = "hola") -> dict[str, str]:
    return {
        "tenant_id": str(TEST_ALPHA_ID),
        "provider": "meta",
        "channel": "whatsapp",
        "external_message_id": message_id,
        "customer_external_id": f"wa-{uuid.uuid4().hex[:6]}",
        "customer_name": "Test",
        "content": content,
        "connection_external_id": PHONE,
        "media": "[]",
        "media_context": "",
    }


async def test_consumer_name_unico_por_proceso() -> None:
    from core.orchestrator.consumer import consumer_name

    name = consumer_name()
    assert name.startswith("orchestrator-")
    # Antes era fijo `orchestrator-1`: dos réplicas no se distinguían.
    assert name != "orchestrator-1"


async def test_claim_duplicate_busy_y_abandonado() -> None:
    """El claim distingue terminado (duplicate), en vuelo reciente (busy) y
    abandonado (se revoca y se reprocesa)."""
    from datetime import timedelta

    from sqlalchemy import update

    from core.db.models import ProcessedEvent, utcnow
    from core.db.session import get_session
    from core.orchestrator.consumer import _claim_event, _mark_event_completed
    from core.shared.tenant import tenant_scope

    tid = TEST_ALPHA_ID
    mid = f"wamid.CLAIM.{uuid.uuid4().hex[:8]}"
    fields = _fields(mid)

    async with tenant_scope(tid):
        assert await _claim_event(tid, fields) == "claimed"
        # Recién reclamado y sin completar: otro worker en vuelo.
        assert await _claim_event(tid, fields) == "busy"
        await _mark_event_completed(tid, mid)
        assert await _claim_event(tid, fields) == "duplicate"

    # Claim abandonado: se envejece `handled_at` más allá del umbral.
    mid2 = f"wamid.CLAIM.{uuid.uuid4().hex[:8]}"
    fields2 = _fields(mid2)
    async with tenant_scope(tid):
        assert await _claim_event(tid, fields2) == "claimed"
    # `processed_events` tiene RLS: el UPDATE necesita el tenant en contexto.
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(ProcessedEvent)
                .where(
                    ProcessedEvent.tenant_id == tid,
                    ProcessedEvent.external_message_id == mid2,
                )
                .values(handled_at=utcnow() - timedelta(days=1))
            )
    async with tenant_scope(tid):
        assert await _claim_event(tid, fields2) == "claimed"


async def test_reclamo_procesa_pendiente_huerfano(monkeypatch) -> None:
    """Un mensaje que un worker muerto dejó pendiente se reclama y se procesa."""
    from sqlalchemy import select

    from core.config import get_settings
    from core.db.models import ProcessedEvent
    from core.db.session import get_session
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis
    from core.orchestrator.consumer import GROUP, _reclaim_pending, ensure_group

    r = get_redis()
    await ensure_group()
    mid = f"wamid.ORPH.{uuid.uuid4().hex[:8]}"
    entry_id = await r.xadd(INBOUND_STREAM, _fields(mid))
    # Un worker "muerto" lo leyó y nunca ackeó.
    await r.xreadgroup(GROUP, "dead-worker", {INBOUND_STREAM: ">"}, count=10)

    monkeypatch.setattr(get_settings(), "inbound_reclaim_idle_ms", 1)
    # El reclamo exige `idle >= min_idle_time`: una espera mínima lo hace determinista.
    await asyncio.sleep(0.02)
    pending: set[asyncio.Task[None]] = set()
    await _reclaim_pending(r, pending)
    assert pending, "el pendiente huérfano debería reclamarse"
    await asyncio.gather(*list(pending), return_exceptions=True)

    # Se ackeó (ya no está pendiente) y el claim quedó terminado.
    still_pending = await r.xpending_range(INBOUND_STREAM, GROUP, min=entry_id, max=entry_id, count=1)
    assert not still_pending
    from core.shared.tenant import tenant_scope

    async with tenant_scope(TEST_ALPHA_ID):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(ProcessedEvent).where(
                        ProcessedEvent.tenant_id == TEST_ALPHA_ID,
                        ProcessedEvent.external_message_id == mid,
                    )
                )
            ).scalar_one_or_none()
    assert row is not None and row.completed_at is not None


async def test_dlq_con_error_y_claim_revocado(monkeypatch) -> None:
    """Tres fallos → DLQ con el error, ACK y claim revocado (reintentable)."""
    from sqlalchemy import select

    from core.db.models import ProcessedEvent
    from core.db.session import get_session
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis
    from core.orchestrator import consumer as mod

    r = get_redis()
    await mod.ensure_group()
    mid = f"wamid.DLQ.{uuid.uuid4().hex[:8]}"
    fields = _fields(mid)
    entry_id = await r.xadd(INBOUND_STREAM, fields)
    await r.xreadgroup(mod.GROUP, mod.CONSUMER, {INBOUND_STREAM: ">"}, count=10)

    async def boom(_fields: dict[str, str]) -> None:
        raise RuntimeError("fallo forzado de prueba")

    monkeypatch.setattr(mod, "_handle_inbound_scoped", boom)
    for _ in range(mod.MAX_DELIVERIES):
        await mod._handle_entry(entry_id, fields)

    # Está en la DLQ con el error y los intentos.
    rows = await r.xrange(mod.DEAD_LETTER_STREAM, min="-", max="+", count=500)
    match = [f for _eid, f in rows if f.get("external_message_id") == mid]
    assert match, "el mensaje debería estar en la DLQ"
    assert "fallo forzado de prueba" in match[-1].get("error", "")
    assert match[-1].get("deliveries") == str(mod.MAX_DELIVERIES)

    # Se ackeó del stream.
    pend = await r.xpending_range(INBOUND_STREAM, mod.GROUP, min=entry_id, max=entry_id, count=1)
    assert not pend

    # El claim quedó revocado (soft-delete) para permitir el reintento manual.
    # Hay una fila por intento: la ÚLTIMA es la que quedó revocada.
    from core.shared.tenant import tenant_scope

    async with tenant_scope(TEST_ALPHA_ID):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(ProcessedEvent)
                    .where(
                        ProcessedEvent.tenant_id == TEST_ALPHA_ID,
                        ProcessedEvent.external_message_id == mid,
                    )
                    .order_by(ProcessedEvent.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    assert row is not None and row.deleted_at is not None

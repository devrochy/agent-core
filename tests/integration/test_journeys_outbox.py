"""El paso `outbox` del runner de journeys (HU-SH-08) entrega solo los eventos del negocio
del journey: nunca toca los de otros negocios de la base."""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
assert _SPEC and _SPEC.loader
journeys = importlib.util.module_from_spec(_SPEC)
sys.modules["journeys"] = journeys
_SPEC.loader.exec_module(journeys)


async def _negocio_con_evento(topic: str) -> tuple[uuid.UUID, uuid.UUID]:
    from sqlalchemy import select

    from core.alta_negocio import onboard_tenant
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.events.outbox import emit_domain
    from core.shared.tenant import tenant_scope

    slug = f"outbox-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Outbox {slug}", slug=slug, persona={"tone": "cercano"})
    async with tenant_scope(tid):
        async with get_session() as session:
            await emit_domain(session, tenant_id=tid, topic=topic, dedupe_key=f"journeys-{slug}", payload={"marca": slug})
        async with get_session() as session:
            event_id = (
                await session.execute(select(DomainEvent.id).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic))
            ).scalar_one()
    return tid, event_id


async def _estado(tid: uuid.UUID, event_id: uuid.UUID) -> str:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (await session.get(DomainEvent, event_id)).status


async def test_el_paso_outbox_solo_entrega_los_eventos_del_negocio_del_journey() -> None:
    from core.shared.events import event_bus

    topic = f"journeys.prueba.{uuid.uuid4().hex[:6]}"
    recibidos: list[str] = []

    async def handler(**payload):
        recibidos.append(payload["marca"])

    event_bus.subscribe(topic, handler)
    try:
        propio, evento_propio = await _negocio_con_evento(topic)
        otro, evento_otro = await _negocio_con_evento(topic)
        ctx = journeys.Contexto(propio, uuid.uuid4(), uuid.uuid4(), journeys.PagoSimulado())
        await journeys.paso_outbox(ctx, {"outbox": "despachar"}, [])
        assert await _estado(propio, evento_propio) == "entregado"
        assert await _estado(otro, evento_otro) == "pendiente"
        assert len(recibidos) == 1
    finally:
        event_bus.unsubscribe_all([handler])


async def test_entregar_evento_con_el_negocio_equivocado_no_entrega() -> None:
    from datetime import UTC, datetime

    from core.tasks.domain_events_dispatch import entregar_evento

    topic = f"journeys.cruzado.{uuid.uuid4().hex[:6]}"
    propio, evento = await _negocio_con_evento(topic)
    otro, _ = await _negocio_con_evento(topic)
    assert await entregar_evento(evento, otro, datetime.now(UTC)) is False
    assert await _estado(propio, evento) == "pendiente"

"""HU-SUB-03 / ADR-0107: outbox transaccional, dedupe y entrega con tenant_scope.

Los escenarios corren con el stack real (AGENT_CORE_INTEGRATION=1) para
verificar RLS, la misma transacción y la entrega del despachador.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

TENANT = TEST_ALPHA_ID


@pytest.mark.asyncio
async def test_el_evento_no_existe_si_la_transaccion_se_revierte() -> None:
    """Gherkin 1: misma transacción — rollback elimina el evento."""
    from sqlalchemy import select

    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.events.outbox import emit_domain
    from core.shared.tenant import tenant_scope

    dedupe = f"rollback:{uuid.uuid4().hex[:12]}"
    async with tenant_scope(TENANT):
        try:
            async with get_session() as session:
                await emit_domain(
                    session,
                    tenant_id=TENANT,
                    topic="payment.confirmed",
                    dedupe_key=dedupe,
                    payload={"reference": "OC-ROLLBACK"},
                )
                raise RuntimeError("forzar rollback")
        except RuntimeError:
            pass

        async with get_session() as session:
            existentes = (
                await session.execute(
                    select(DomainEvent.id).where(DomainEvent.dedupe_key == dedupe)
                )
            ).scalars().all()
    assert existentes == []


@pytest.mark.asyncio
async def test_dedupe_key_evita_duplicados() -> None:
    """Gherkin 3: dos escrituras con la misma clave → una sola fila."""
    from sqlalchemy import select

    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.events.outbox import emit_domain
    from core.shared.tenant import tenant_scope

    dedupe = f"dedupe:{uuid.uuid4().hex[:12]}"
    async with tenant_scope(TENANT):
        async with get_session() as session:
            primera = await emit_domain(
                session, tenant_id=TENANT, topic="payment.confirmed",
                dedupe_key=dedupe, payload={"reference": "OC-0001"},
            )
            segunda = await emit_domain(
                session, tenant_id=TENANT, topic="payment.confirmed",
                dedupe_key=dedupe, payload={"reference": "OC-0001"},
            )
        async with get_session() as session:
            total = (
                await session.execute(
                    select(DomainEvent.id).where(DomainEvent.dedupe_key == dedupe)
                )
            ).scalars().all()
    assert primera is True
    assert segunda is False
    assert len(total) == 1


@pytest.mark.asyncio
async def test_despachador_entrega_con_el_tenant_del_evento() -> None:
    """Gherkin 4: el handler corre bajo el tenant_scope del evento (cross-tenant)."""
    from core.db.session import get_session
    from core.events.outbox import emit_domain
    from core.shared.events import event_bus
    from core.shared.tenant import get_current_tenant_id, tenant_scope
    from core.tasks.domain_events_dispatch import dispatch_due_events

    tenant_b = uuid.uuid4()
    registros: list[tuple[str, str]] = []

    async def _handler(reference: str, amount: float) -> None:
        registros.append((reference, str(get_current_tenant_id())))

    event_bus.subscribe("payment.confirmed", _handler)
    try:
        async with tenant_scope(tenant_b):
            async with get_session() as session:
                await emit_domain(
                    session,
                    tenant_id=tenant_b,
                    topic="payment.confirmed",
                    dedupe_key=f"cross:{uuid.uuid4().hex[:12]}",
                    payload={"reference": "OC-CROSS", "amount": 1.0},
                )

        # La base de la suite completa trae eventos pendientes de otros tests (más de un lote;
        # cada aviso del centro de notificaciones deja uno, ADR-0167): se despacha por lotes
        # hasta que llega el de este test.
        procesados = 0
        for _ in range(200):
            lote = await dispatch_due_events(limit=200)
            procesados += lote
            if lote == 0 or any(ref == "OC-CROSS" for ref, _tid in registros):
                break
        assert procesados >= 1
        assert any(ref == "OC-CROSS" and tid == str(tenant_b) for ref, tid in registros)
    finally:
        event_bus.unsubscribe_all([_handler])

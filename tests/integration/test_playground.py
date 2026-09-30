"""Playground "Probar asistente" (Fase CT) contra el stack real.

Valida el aislamiento: una conversación de prueba (`channel == "test"`) no
consume la cuota de `conversaciones` ni crea `Escalation`.

Requiere el stack local y `AGENT_CORE_INTEGRATION=1` (ver `make test-integration`;
conviene detener `core-api` antes: su consumer comparte el group de Redis).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_playground_no_consume_cuota_ni_escala(tenant_setup) -> None:
    t1, _t2 = tenant_setup
    from core.admin import playground
    from core.billing.metrics import count_metric
    from core.db.models import Conversation, Escalation
    from core.db.session import get_session
    from core.orchestrator.escalation import escalate
    from core.shared.tenant import tenant_scope
    from core.shared.test_mode import TEST_CHANNEL

    async with tenant_scope(t1):
        before = await count_metric(t1, "conversaciones")

    async with tenant_scope(t1):
        conv = await playground.get_or_create_test_conversation(t1)
        assert conv.channel == TEST_CHANNEL
        # Idempotente: reusar no crea una segunda prueba viva.
        again = await playground.get_or_create_test_conversation(t1)
        assert again.id == conv.id

    async with tenant_scope(t1):
        after = await count_metric(t1, "conversaciones")
    assert after == before, "una conversación de prueba no debe contar para la cuota"

    # Escalar en una prueba no crea Escalation ni pausa la conversación.
    async with tenant_scope(t1):
        await escalate(conv.id, reason="playground_test", summary="test")
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Escalation).where(Escalation.conversation_id == conv.id)
                )
            ).scalars().all()
        assert rows == []

    # Limpieza: cerrar la prueba para no dejarla como "viva".
    async with tenant_scope(t1):
        async with get_session() as session:
            row = await session.get(Conversation, conv.id)
            if row is not None:
                row.status = "closed"

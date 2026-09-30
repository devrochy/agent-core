"""Límites del tool loop contra el stack real (Sprint 2, Fase B, ADR-0015)."""

from __future__ import annotations

import os
import types
import uuid
from typing import Any, ClassVar

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


class ToolUseResponse:
    stop_reason = "tool_use"
    content = ""
    tool_calls: ClassVar[list[Any]] = []
    model = "fake-tool"
    latency_ms = 1
    usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)


class ToolUseProvider:
    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> ToolUseResponse:
        self.calls += 1
        resp = ToolUseResponse()
        resp.tool_calls = [types.SimpleNamespace(id="c1", name="get_customer_profile", arguments={})]
        return resp

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        cin, cout = self.pricing_per_mtok
        return (input_tokens * cin + output_tokens * cout) / 1_000_000


async def _make_conversation(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"lim:{uuid.uuid4().hex[:8]}")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp")
            session.add(conv)
            await session.flush()
            return conv.id


async def _set_spec_policies(tid: uuid.UUID, **policies: Any) -> None:
    import copy

    from sqlalchemy import select

    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tid,
                        AgentSpecRecord.is_active.is_(True),
                    )
                )
            ).scalar_one()
            # deepcopy: mutar JSONB anidado requiere copia profunda para que
            # SQLAlchemy detecte el cambio.
            spec = copy.deepcopy(row.spec)
            spec.setdefault("policies", {}).update(policies)
            row.spec = spec


async def _seed_spent(tid: uuid.UUID, conversation_id: uuid.UUID, cost: float) -> None:
    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=tid,
                    conversation_id=conversation_id,
                    model="fake",
                    input_tokens=100,
                    output_tokens=100,
                    cost_usd=cost,
                )
            )


async def test_budget_exhausted_corta_el_turno(tenant_setup, recorded, monkeypatch) -> None:
    t1, _t2 = tenant_setup
    recorded.clear()
    conversation_id = await _make_conversation(t1)
    await _set_spec_policies(t1, budgetUsdPerConversation=0.01)
    await _seed_spent(t1, conversation_id, 0.02)  # ya superó el presupuesto

    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        result = await process_conversation(conversation_id, "hola")

    assert result is not None and result.text
    assert "presupuesto" in result.text.lower()
    assert len(recorded) == 0, "no debe llamar al LLM con presupuesto agotado"

    # la conversación queda marcada: el siguiente inbound no gasta
    async with tenant_scope(t1):
        second = await process_conversation(conversation_id, "hola de nuevo")
    assert second is None, "con presupuesto agotado el turno siguiente debe ser silencioso"
    assert len(recorded) == 0


async def test_max_tool_calls_corta_el_turno(tenant_setup, monkeypatch) -> None:
    t1, _t2 = tenant_setup
    conversation_id = await _make_conversation(t1)
    await _set_spec_policies(t1, maxToolCallsPerTurn=1)

    provider = ToolUseProvider()
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: provider)

    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        result = await process_conversation(conversation_id, "hola")

    assert provider.calls == 1, "maxToolCallsPerTurn=1 debe cortar a 1 llamada"
    assert result is not None and result.escalated, "el loop agotado debe escalar"
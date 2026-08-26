"""usage_ledger y costos por tenant contra el stack real (Sprint 2, Fase C, ADR-0008)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func, select, text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _make_conversation(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"led:{uuid.uuid4().hex[:8]}")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp")
            session.add(conv)
            await session.flush()
            return conv.id


async def _count_ledger(tid: uuid.UUID, conversation_id: uuid.UUID) -> int:
    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(
                    select(func.count(UsageLedger.id)).where(UsageLedger.conversation_id == conversation_id)
                )
            ).scalar() or 0


async def test_un_turno_escribe_una_fila_ledger(tenant_setup) -> None:
    t1, _t2 = tenant_setup
    conversation_id = await _make_conversation(t1)

    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        result = await process_conversation(conversation_id, "hola")
    assert result is not None and result.text

    rows = await _count_ledger(t1, conversation_id)
    assert rows == 1, f"1 turno debe escribir exactamente 1 fila del ledger, hay {rows}"

    from core.db.models import UsageLedger
    from core.db.session import get_session

    async with tenant_scope(t1):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(UsageLedger).where(UsageLedger.conversation_id == conversation_id)
                )
            ).scalar_one()
    assert row.agent_spec_version == "1.0.0"
    assert row.model  # modelo del turno
    assert row.input_tokens > 0
    assert float(row.cost_usd) >= 0


async def test_ledger_es_append_only(tenant_setup) -> None:
    t1, _t2 = tenant_setup
    conversation_id = await _make_conversation(t1)

    from sqlalchemy.exc import DBAPIError

    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=t1,
                    conversation_id=conversation_id,
                    model="fake",
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=0.001,
                )
            )

    # el trigger inmutable debe rechazar el UPDATE
    async with tenant_scope(t1):
        async with get_session() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text("update usage_ledger set cost_usd = 0 where model = 'fake'"))
                await session.commit()


async def test_costos_aislados_por_tenant(tenant_setup) -> None:
    t1, t2 = tenant_setup
    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    conv_t1 = await _make_conversation(t1)
    conv_t2 = await _make_conversation(t2)

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add(UsageLedger(tenant_id=t1, conversation_id=conv_t1, model="m", input_tokens=1, output_tokens=1, cost_usd=0.10))
    async with tenant_scope(t2):
        async with get_session() as session:
            session.add(UsageLedger(tenant_id=t2, conversation_id=conv_t2, model="m", input_tokens=1, output_tokens=1, cost_usd=0.99))

    # aislamiento por conversación: T1 no ve el ledger de T2 y viceversa
    async with tenant_scope(t1):
        async with get_session() as session:
            visible_t2 = (
                await session.execute(
                    select(func.count(UsageLedger.id)).where(UsageLedger.conversation_id == conv_t2)
                )
            ).scalar()
    async with tenant_scope(t2):
        async with get_session() as session:
            visible_t1 = (
                await session.execute(
                    select(func.count(UsageLedger.id)).where(UsageLedger.conversation_id == conv_t1)
                )
            ).scalar()
    async with get_session() as session:
        total_global = (await session.execute(select(func.count(UsageLedger.id)))).scalar()

    assert visible_t2 == 0, "T1 NO debe ver el ledger de T2"
    assert visible_t1 == 0, "T2 NO debe ver el ledger de T1"
    assert total_global == 0, "sin contexto de tenant no debe ver ningún ledger"
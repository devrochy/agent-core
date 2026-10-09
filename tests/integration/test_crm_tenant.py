"""crm_* multi-tenant contra el stack real (Sprint 3, Fase A)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func, select, text

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_crm_aislado_por_tenant(tenant_setup) -> None:
    t1, t2 = tenant_setup
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    # limpia sus propios leads de prueba
    for tid in (t1, t2):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(Lead.__table__.delete().where(Lead.name.like("rls-crm:%")))

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add(Lead(name="rls-crm:t1", interest="x"))
    async with tenant_scope(t2):
        async with get_session() as session:
            session.add(Lead(name="rls-crm:t2", interest="x"))

    async with tenant_scope(t1):
        async with get_session() as session:
            names = (await session.execute(select(Lead.name))).scalars().all()
    assert "rls-crm:t1" in names
    assert "rls-crm:t2" not in names, "T1 NO debe ver los leads de T2"

    # sin contexto de tenant: RLS fail-closed
    async with get_session() as session:
        n = (await session.execute(select(func.count()).select_from(Lead))).scalar()
    assert n == 0, "sin contexto no debe ver ningún lead"


async def test_insert_crm_sin_scope_falla_claro(tenant_setup) -> None:
    from pack_commerce.models import _tenant_or_raise

    from core.shared.exceptions import PluginError
    from core.shared.tenant import get_current_tenant_id

    # fuera de tenant_scope, el default del modelo debe fallar con error claro
    assert get_current_tenant_id() is None
    with pytest.raises(PluginError) as excinfo:
        _tenant_or_raise()
    assert "sin tenant_scope" in str(excinfo.value), "debe fallar con mensaje claro"


async def test_numeracion_pedidos_por_tenant(tenant_setup) -> None:
    t1, t2 = tenant_setup
    from pack_commerce.models import Order

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    # reset de pedidos y contadores por tenant (idempotencia entre corridas)
    for tid in (t1, t2):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(text("delete from pack_commerce_order_items"))
                await session.execute(text("delete from pack_commerce_orders"))
                await session.execute(
                    text("delete from pack_commerce_order_counters where tenant_id = :t"), {"t": tid}
                )

    # contadores independientes por tenant
    async with tenant_scope(t1):
        async with get_session() as session:
            s1 = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": t1})).scalar()
            s2 = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": t1})).scalar()
    async with tenant_scope(t2):
        async with get_session() as session:
            s3 = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": t2})).scalar()
    assert (s1, s2) == (1, 2), "T1 debe numerar desde 1"
    assert s3 == 1, "T2 debe numerar desde 1 (independiente)"

    # dos tenants pueden tener ambos OC-0001 sin colisión (UNIQUE(tenant, seq))
    async with tenant_scope(t1):
        async with get_session() as session:
            cust = Customer(tenant_id=t1, channel="whatsapp", external_id=f"num:{uuid.uuid4().hex[:8]}")
            session.add(cust)
            await session.flush()
            conv = Conversation(tenant_id=t1, customer_id=cust.id, channel="whatsapp")
            session.add(conv)
            await session.flush()
            order = Order(
                quote_id=None,
                conversation_id=conv.id,
                customer_id=cust.id,
                idempotency_key=f"num:{uuid.uuid4().hex[:8]}",
                order_seq=1,
                public_order_number="OC-0001",
                total=10,
            )
            session.add(order)
            await session.flush()
            assert order.public_order_number == "OC-0001"

    async with tenant_scope(t2):
        async with get_session() as session:
            cust2 = Customer(tenant_id=t2, channel="whatsapp", external_id=f"num:{uuid.uuid4().hex[:8]}")
            session.add(cust2)
            await session.flush()
            conv2 = Conversation(tenant_id=t2, customer_id=cust2.id, channel="whatsapp")
            session.add(conv2)
            await session.flush()
            session.add(
                Order(
                    quote_id=None,
                    conversation_id=conv2.id,
                    customer_id=cust2.id,
                    idempotency_key=f"num:{uuid.uuid4().hex[:8]}",
                    order_seq=1,
                    public_order_number="OC-0001",
                    total=20,
                )
            )

    # T1 no ve el pedido de T2
    async with tenant_scope(t1):
        async with get_session() as session:
            t2_visible = (
                await session.execute(
                    select(func.count()).select_from(Order).where(Order.total == 20)
                )
            ).scalar()
    assert t2_visible == 0, "T1 no debe ver el pedido de T2"
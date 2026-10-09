"""HU-COM-02: umbral por tipo de producto en `crear_pedido`.

Un plan recurrente (variante con `billing_period`) no escala por monto alto
por defecto; un pedido one-shot con el mismo total sí.
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

T1 = TEST_ALPHA_ID


@pytest.fixture
def ctx():
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


async def _quote_con_variante(session, *, total: float, billing_period: str | None, customer_id):
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem

    sku = f"PLAN-{uuid.uuid4().hex[:6]}"
    product = Product(tenant_id=T1, name=f"Plan {sku}")
    session.add(product)
    await session.flush()
    variant = ProductVariant(
        tenant_id=T1, product_id=product.id, sku=sku, price=total, billing_period=billing_period
    )
    session.add(variant)
    await session.flush()
    quote = Quote(
        tenant_id=T1, conversation_id=uuid.uuid4(), customer_id=customer_id,
        subtotal=total, discount=0, total=total, status="aceptada",
    )
    session.add(quote)
    await session.flush()
    session.add(
        QuoteItem(
            tenant_id=T1, quote_id=quote.id, variant_id=variant.id, quantity=1, unit_price=total
        )
    )
    await session.flush()
    return quote


async def _crear_pedido(quote_id: str, ctx):
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        return await OrderSkill().crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "efectivo_contraentrega",
                "delivery_method": "domicilio",
                "customer_data": {
                    "full_name": "Test", "id_document": "1", "address": "Calle 1",
                    "municipality": "Manizales", "city": "Manizales",
                },
            },
            ctx,
        )


async def test_plan_de_1_8m_no_escala_por_defecto(ctx) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _quote_con_variante(
                session, total=1_800_000.0, billing_period="mensual", customer_id=ctx.customer_id
            )
            quote_id = str(quote.id)

    result = await _crear_pedido(quote_id, ctx)
    assert result.get("status") == "confirmed", result
    assert "escalate" not in result


async def test_oneshot_de_1_8m_si_escala(ctx) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _quote_con_variante(
                session, total=1_800_000.0, billing_period=None, customer_id=ctx.customer_id
            )
            quote_id = str(quote.id)

    result = await _crear_pedido(quote_id, ctx)
    assert result.get("escalate") is True, result

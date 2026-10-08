"""Un plan recurrente se vende sin inventario (encontrado por los journeys de HU-SUB-12).

Con stock 0 (como el catálogo de `solvershub`), `consultar_inventario` lo marcaba no
disponible y `generar_cotizacion` lo rechazaba: el asistente no podía vender planes.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


def _ctx():
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}", channel="whatsapp",
    )


async def _plan_con_stock_cero() -> str:
    from pack_commerce.models import Inventory, Product, ProductVariant

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sku = f"PLAN-{uuid.uuid4().hex[:6].upper()}"
    async with tenant_scope(T1):
        async with get_session() as s:
            product = Product(tenant_id=T1, name=f"Plan {sku}")
            s.add(product)
            await s.flush()
            variant = ProductVariant(tenant_id=T1, product_id=product.id, sku=sku, price=90000, billing_period="mensual")
            s.add(variant)
            await s.flush()
            s.add(Inventory(tenant_id=T1, variant_id=variant.id, stock_quantity=0))
    return sku


async def test_el_plan_figura_disponible_y_se_cotiza_sin_stock() -> None:
    from pack_commerce.skills import CatalogSkill, OrderSkill

    from core.shared.tenant import tenant_scope

    sku = await _plan_con_stock_cero()
    async with tenant_scope(T1):
        inventario = await CatalogSkill().consultar_inventario({"query": sku}, _ctx())
        cotizacion = await OrderSkill().generar_cotizacion({"items": [{"variant_id": sku, "quantity": 1}]}, _ctx())
    match = next(m for m in inventario["matches"] if m["sku"] == sku)
    assert match["disponible"] is True
    assert match["stock"] is None
    assert "error" not in cotizacion, cotizacion
    assert float(cotizacion["total"]) == 90000


async def test_un_pedido_de_plan_no_descuenta_inventario() -> None:
    from pack_commerce.models import Inventory, ProductVariant
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sku = await _plan_con_stock_cero()
    async with tenant_scope(T1):
        async with get_session() as s:
            variant_id = (await s.execute(select(ProductVariant.id).where(ProductVariant.sku == sku))).scalar_one()
            await OrderSkill._decrement_stock(s, variant_id, 1)
        async with get_session() as s:
            stocks = (
                await s.execute(select(Inventory.stock_quantity).where(Inventory.variant_id == variant_id))
            ).scalars().all()
    assert stocks == [0]  # ni descuento ni fila nueva

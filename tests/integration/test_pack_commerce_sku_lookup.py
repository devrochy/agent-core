"""`generar_cotizacion`/`agregar_item_pedido` deben aceptar el SKU del
producto directamente en `variant_id`, no solo el UUID real interno.

Hallazgo (2026-09-12): investigando por qué `venta-generar-cotizacion-04`
y `venta-agregar-item-pedido-23` fallaban de forma consistente en el gate
de evals (no solo el flake ya conocido de `venta-recomendar-producto-03`),
se confirmó la causa real: el escenario le da al modelo un SKU concreto
("A-001") pero nunca un UUID real -- y ambas tools solo aceptaban un UUID
real como `variant_id`. El modelo, correctamente, se negaba a inventar un
UUID y llamaba `consultar_inventario` primero para conseguirlo -- un
comportamiento correcto (invariante "nunca inventes IDs"), pero que la
tool debería resolver ella misma en vez de forzar un paso previo. Un
cliente real casi siempre se refiere a un producto por su SKU, no por su
UUID interno.

`_get_variant_with_stock` ahora resuelve por `id` real O por `sku`
exacto -- sin exigir que el LLM haya consultado antes. Ojo con la
corrección relacionada: el `variant_id` guardado en `QuoteItem`/`OrderItem`
tiene que ser SIEMPRE el UUID real resuelto, nunca el string que mandó el
LLM (si mandó un SKU, guardarlo tal cual en una columna UUID revienta con
`asyncpg.exceptions.DataError` -- el mismo incidente real que ya cubre
`test_pack_commerce_invalid_id_handling.py`).
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID, TEST_BETA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID
T2 = TEST_BETA_ID


async def _make_variant(
    session, *, sku: str, price: float = 20_000.0, stock: int = 50, tenant_id=T1
):
    from pack_commerce.models import Inventory, Product, ProductVariant

    product = Product(tenant_id=tenant_id, name=f"Producto {sku}")
    session.add(product)
    await session.flush()
    variant = ProductVariant(tenant_id=tenant_id, product_id=product.id, sku=sku, price=price)
    session.add(variant)
    await session.flush()
    session.add(Inventory(tenant_id=tenant_id, variant_id=variant.id, stock_quantity=stock))
    await session.flush()
    return variant


@pytest.fixture
def fake_tool_context():
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


async def test_get_variant_with_stock_resuelve_por_sku():
    from pack_commerce.skills import _get_variant_with_stock

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(T1):
        async with get_session() as session:
            variant = await _make_variant(session, sku=sku, price=30_000.0, stock=5)
            variant_id = variant.id

        data = await _get_variant_with_stock(sku)
        assert data is not None, data
        assert data["variant"].id == variant_id
        assert data["price"] == 30_000.0
        assert data["stock"] == 5


async def test_get_variant_with_stock_sku_inexistente_retorna_none():
    from pack_commerce.skills import _get_variant_with_stock

    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        data = await _get_variant_with_stock(f"NO-EXISTE-{uuid.uuid4().hex[:8]}")
    assert data is None


async def test_get_variant_with_stock_string_arbitrario_no_revienta():
    """Caso exacto del incidente real (tenant `yopoldo`, 2026-09-11, ver
    `tests/test_pack_commerce_invalid_id_handling.py`): un string que no es
    ni un UUID ni un SKU real -- antes se descartaba antes de tocar la BD
    (`_parse_uuid` bastaba); ahora que también se busca por SKU, tiene que
    resolverse sin excepción igual, solo que consultando la BD."""
    from pack_commerce.skills import _get_variant_with_stock

    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        data = await _get_variant_with_stock("pollo_broaster")
    assert data is None


async def test_get_variant_with_stock_sku_no_cruza_tenant():
    """Hallazgo de `auditor-multitenant` (2026-09-12): el lookup por SKU
    filtra `tenant_id` explícito en Python además de RLS -- esta es la
    prueba de regresión que faltaba para ese caso. Dos tenants pueden tener
    el MISMO SKU (el índice único es `(tenant_id, sku)`, no global) sin
    pisarse ni ver el producto ajeno."""
    from pack_commerce.skills import _get_variant_with_stock

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(T1):
        async with get_session() as session:
            variant_t1 = await _make_variant(session, sku=sku, price=10_000.0, tenant_id=T1)
            variant_t1_id = variant_t1.id

    # T2 nunca creó ese SKU: no debería verlo, aunque exista para T1.
    async with tenant_scope(T2):
        data = await _get_variant_with_stock(sku)
    assert data is None

    # Si T2 registra el MISMO SKU (permitido: la unicidad es por tenant),
    # cada tenant debe ver únicamente su propia variante.
    async with tenant_scope(T2):
        async with get_session() as session:
            variant_t2 = await _make_variant(session, sku=sku, price=99_999.0, tenant_id=T2)
            variant_t2_id = variant_t2.id

    async with tenant_scope(T1):
        data_t1 = await _get_variant_with_stock(sku)
    async with tenant_scope(T2):
        data_t2 = await _get_variant_with_stock(sku)

    assert data_t1["variant"].id == variant_t1_id
    assert data_t1["price"] == 10_000.0
    assert data_t2["variant"].id == variant_t2_id
    assert data_t2["price"] == 99_999.0


async def test_generar_cotizacion_acepta_sku_directo(fake_tool_context):
    """El SKU llega tal cual en `variant_id` -- la tool lo resuelve y guarda
    el UUID real en `QuoteItem`, no el string del SKU."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(T1):
        async with get_session() as session:
            variant = await _make_variant(session, sku=sku, price=25_000.0, stock=10)
            variant_id = variant.id

        skill = OrderSkill()
        result = await skill.generar_cotizacion(
            {"items": [{"variant_id": sku, "quantity": 2}]}, fake_tool_context
        )

    assert "error" not in result, result
    assert result.get("total") == 50_000.0, result

    from pack_commerce.models import Quote, QuoteItem

    async with tenant_scope(T1):
        async with get_session() as session:
            quote_id = result["quote_id"]
            quote = await session.get(Quote, quote_id)
            assert quote is not None
            item = (
                await session.execute(
                    QuoteItem.__table__.select().where(QuoteItem.quote_id == quote_id)
                )
            ).first()
            assert item is not None
            assert str(item.variant_id) == str(variant_id), (
                "QuoteItem.variant_id debe ser el UUID real resuelto, no el SKU"
            )


async def test_agregar_item_pedido_acepta_sku_directo(fake_tool_context):
    """Mismo caso que generar_cotizacion, pero agregando a un pedido ya
    abierto (`agregar_item_pedido`)."""
    from pack_commerce.skills import OrderSkill
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(T1):
        async with get_session() as session:
            variant = await _make_variant(session, sku=sku, price=15_000.0, stock=10)
            variant_id = variant.id

            from pack_commerce.models import Order

            order_seq = (
                await session.execute(
                    text("select pack_commerce_next_order_seq(:t)"), {"t": T1}
                )
            ).scalar()
            order = Order(
                conversation_id=uuid.uuid4(),
                customer_id=uuid.uuid4(),
                status="abierto",
                payment_method="efectivo_contraentrega",
                payment_status="pendiente",
                delivery_method="domicilio",
                total=0,
                order_seq=order_seq,
                idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
            )
            session.add(order)
            await session.flush()
            order_id = str(order.id)

        skill = OrderSkill()
        result = await skill.agregar_item_pedido(
            {"order_id": order_id, "items": [{"variant_id": sku, "quantity": 1}]},
            fake_tool_context,
        )

    assert "error" not in result, result
    assert result.get("status") == "updated", result
    assert result["agregados"] == [{"sku": sku, "quantity": 1}], result

    from pack_commerce.models import Order, OrderItem

    async with tenant_scope(T1):
        async with get_session() as session:
            item = (
                await session.execute(
                    OrderItem.__table__.select().where(OrderItem.order_id == order_id)
                )
            ).first()
            assert item is not None
            assert str(item.variant_id) == str(variant_id)

            # Limpieza: no dejar el pedido/ítem reales en la BD compartida de tests.
            await session.execute(OrderItem.__table__.delete().where(OrderItem.order_id == order_id))
            await session.execute(Order.__table__.delete().where(Order.id == order_id))

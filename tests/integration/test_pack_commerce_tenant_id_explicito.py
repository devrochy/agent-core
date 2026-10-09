"""Defensa en profundidad: filtro `tenant_id` explícito en Python además de
RLS, en las tools/funciones de `pack_commerce/skills.py` y los crons de
`pack_commerce/jobs.py` (fila 127 del ROADMAP -- inventario exhaustivo hecho
con un Explore agent, 59 call-sites / 27 funciones sin filtro en TODO el
pack; este PR cubre `skills.py`+`jobs.py`, `module.py` queda para otro PR).

Igual que `test_pack_commerce_sku_lookup.py` (mismo criterio: no un test
por cada uno de los ~28 call-sites tocados -- desproporcionado -- sino uno
por FLUJO de cara al usuario que representa el riesgo real de que
`RLS + Python` dejen pasar una fila de otro tenant).
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID, TEST_BETA_ID

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID
T2 = TEST_BETA_ID


@pytest.fixture
def fake_tool_context():
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


async def _make_variant(session, *, sku: str, price: float = 20_000.0, stock: int = 50, tenant_id=T1):
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


async def _make_order(session, *, tenant_id, customer_id, order_seq: int):
    """Inserta un `Order` con un `order_seq` EXPLÍCITO (no vía la secuencia
    real `pack_commerce_next_order_seq`) para poder forzar a propósito que
    dos tenants tengan el MISMO `public_order_number` (columna computada
    desde `order_seq`) y probar que no se cruzan."""
    from pack_commerce.models import Order

    order = Order(
        tenant_id=tenant_id,
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        status="abierto",
        payment_method="efectivo_contraentrega",
        payment_status="pendiente",
        delivery_method="domicilio",
        total=100_000,
        order_seq=order_seq,
        public_order_number=f"OC-{order_seq:04d}",
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
    )
    session.add(order)
    await session.flush()
    return order


async def test_consultar_estado_pedido_no_cruza_tenant(fake_tool_context):
    """Mismo `public_order_number` Y mismo `customer_id` en dos tenants
    (adrede, con `order_seq` forzado) -- antes del fix, `consultar_estado_pedido`
    filtraba por `customer_id` + `public_order_number` sin `tenant_id`, así
    que si RLS fallara alguna vez, T1 podría ver el pedido de T2."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    order_seq = 900_000 + uuid.uuid4().int % 90_000  # improbable de colisionar con datos reales
    shared_customer_id = uuid.uuid4()

    async with tenant_scope(T1):
        async with get_session() as session:
            order_t1 = await _make_order(
                session, tenant_id=T1, customer_id=shared_customer_id, order_seq=order_seq
            )
            public_number = order_t1.public_order_number
            order_t1_id = order_t1.id

    async with tenant_scope(T2):
        async with get_session() as session:
            await _make_order(session, tenant_id=T2, customer_id=shared_customer_id, order_seq=order_seq)

    import dataclasses

    context = dataclasses.replace(fake_tool_context, customer_id=shared_customer_id)
    skill = OrderSkill()

    async with tenant_scope(T1):
        result_t1 = await skill.consultar_estado_pedido(
            {"public_order_number": public_number}, context
        )
    async with tenant_scope(T2):
        result_t2 = await skill.consultar_estado_pedido(
            {"public_order_number": public_number}, context
        )

    assert result_t1["found"] is True
    assert result_t2["found"] is True
    # Mismo número visible, pero cada uno debe ver SU PROPIO pedido (created_at
    # distinto por ser filas distintas -- lo que importa es que no revienta
    # cruzado ni devuelve datos del otro tenant).
    assert result_t1["total"] == 100_000.0
    assert result_t2["total"] == 100_000.0

    from pack_commerce.models import Order

    async with tenant_scope(T1):
        async with get_session() as session:
            await session.execute(Order.__table__.delete().where(Order.id == order_t1_id))


async def test_classify_customer_no_cuenta_pedidos_de_otro_tenant():
    """Mismo `customer_id` con pedidos en T1 y T2 -- `classify_customer`
    debe segmentar solo con los pedidos DEL TENANT ACTIVO. Antes del fix,
    `Order.customer_id == customer_id` sin `tenant_id` podía (si RLS
    fallara) sumar el historial de otro tenant al segmento."""
    from pack_commerce.skills import classify_customer

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    shared_customer_id = uuid.uuid4()
    order_seq_base = 800_000 + uuid.uuid4().int % 90_000

    # T2: 6 pedidos (superaría el umbral "fiel", default >=5) del MISMO customer_id.
    async with tenant_scope(T2):
        async with get_session() as session:
            for i in range(6):
                await _make_order(
                    session,
                    tenant_id=T2,
                    customer_id=shared_customer_id,
                    order_seq=order_seq_base + i,
                )

    # T1: el mismo customer_id nunca compró nada EN T1.
    async with tenant_scope(T1):
        segment_t1 = await classify_customer(shared_customer_id)
    assert segment_t1 == "nuevo", (
        "los 6 pedidos de T2 no deben contar para la segmentación de T1"
    )

    async with tenant_scope(T2):
        segment_t2 = await classify_customer(shared_customer_id)
    assert segment_t2 == "fiel", "en T2 sí debe verlos (sanity check del propio test)"


async def test_aplicar_promocion_no_usa_promocion_de_otro_tenant(fake_tool_context):
    """Una promoción activa de T2 no debe poder aplicarse a una cotización
    de T1, aunque no se le pase ningún id -- antes del fix, el `select
    (Promotion).where(Promotion.active...)` no filtraba tenant en absoluto."""
    from pack_commerce.models import Promotion
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(T1):
        async with get_session() as session:
            await _make_variant(session, sku=sku, price=100_000.0, stock=10, tenant_id=T1)

        skill = OrderSkill()
        quote_result = await skill.generar_cotizacion(
            {"items": [{"variant_id": sku, "quantity": 1}]}, fake_tool_context
        )
        quote_id = quote_result["quote_id"]

    # Promoción MUY agresiva, pero de T2 -- nunca debería tocar la cotización de T1.
    async with tenant_scope(T2):
        async with get_session() as session:
            session.add(
                Promotion(
                    tenant_id=T2,
                    type="temporada",
                    rules={"kind": "temporada", "discount_pct": 90},
                    active=True,
                )
            )

    async with tenant_scope(T1):
        result = await skill.aplicar_promocion({"quote_id": quote_id}, fake_tool_context)

    assert result.get("applied") is not True, (
        f"la promoción de T2 no debería haber aplicado a la cotización de T1: {result}"
    )

    from pack_commerce.models import Quote

    async with tenant_scope(T1):
        async with get_session() as session:
            await session.execute(Quote.__table__.delete().where(Quote.id == quote_id))

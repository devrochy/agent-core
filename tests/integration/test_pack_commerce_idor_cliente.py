"""IDOR dentro del tenant: un cliente no toca la cotización ni el pedido de otro.

El `quote_id`/`order_id` lo pasa el LLM. Antes del fix, las tools filtraban
solo por `tenant_id`: un cliente que conociera el UUID de la cotización de
OTRO cliente del mismo negocio podía confirmarla, aplicarle una promoción o
convertirla en pedido (auditor-multitenant, HU-SUB-07). Ahora la ajena
responde igual que una inexistente.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID

DATOS_CLIENTE = {
    "full_name": "Test", "id_document": "1", "address": "Calle 1",
    "municipality": "Manizales", "city": "Manizales",
}


def _ctx(customer_id: uuid.UUID):
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


async def _quote_de(customer_id: uuid.UUID, *, status: str, total: float = 50_000.0) -> str:
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            sku = f"IDOR-{uuid.uuid4().hex[:6]}"
            product = Product(tenant_id=T1, name=f"Producto {sku}")
            session.add(product)
            await session.flush()
            variant = ProductVariant(tenant_id=T1, product_id=product.id, sku=sku, price=total)
            session.add(variant)
            await session.flush()
            quote = Quote(
                tenant_id=T1, conversation_id=uuid.uuid4(), customer_id=customer_id,
                subtotal=total, discount=0, total=total, status=status,
            )
            session.add(quote)
            await session.flush()
            session.add(
                QuoteItem(
                    tenant_id=T1, quote_id=quote.id, variant_id=variant.id, quantity=1, unit_price=total
                )
            )
            return str(quote.id)


async def _order_de(customer_id: uuid.UUID, *, payment_status: str = "pendiente") -> str:
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            order_seq = (
                await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": T1})
            ).scalar()
            order = Order(
                tenant_id=T1, quote_id=None, conversation_id=uuid.uuid4(), customer_id=customer_id,
                status="abierto", payment_status=payment_status, payment_method="pago_en_linea",
                delivery_method="domicilio", total=185_000.0,
                idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
                order_seq=int(order_seq), public_order_number=f"OC-{int(order_seq):04d}",
            )
            session.add(order)
            await session.flush()
            return str(order.id)


async def _numero_de(order_id: str) -> str:
    from pack_commerce.models import Order

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            return (await session.get(Order, uuid.UUID(order_id))).public_order_number


async def _estado_quote(quote_id: str) -> str:
    from pack_commerce.models import Quote

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            return (await session.get(Quote, uuid.UUID(quote_id))).status


async def _pedidos_de_la_quote(quote_id: str) -> int:
    from pack_commerce.models import Order
    from sqlalchemy import func, select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            return (
                await session.execute(
                    select(func.count()).select_from(Order).where(Order.quote_id == uuid.UUID(quote_id))
                )
            ).scalar_one()


async def _crear_pedido(quote_id: str, ctx) -> dict:
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        return await OrderSkill().crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "efectivo_contraentrega",
                "delivery_method": "domicilio",
                "customer_data": DATOS_CLIENTE,
            },
            ctx,
        )


def _cotizacion_no_encontrada() -> dict:
    """Ajena o inexistente responden igual: no se revela que la cotización existe."""
    from pack_commerce.skills import _sin_cotizacion_vigente as respuesta

    return respuesta()

async def test_no_confirma_la_cotizacion_de_otro_cliente() -> None:
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    quote_id = await _quote_de(uuid.uuid4(), status="draft")

    async with tenant_scope(T1):
        result = await OrderSkill().confirmar_cotizacion({"quote_id": quote_id}, _ctx(uuid.uuid4()))

    assert result == _cotizacion_no_encontrada()
    assert await _estado_quote(quote_id) == "draft"


async def test_no_convierte_en_pedido_la_cotizacion_de_otro_cliente() -> None:
    quote_id = await _quote_de(uuid.uuid4(), status="aceptada")

    result = await _crear_pedido(quote_id, _ctx(uuid.uuid4()))

    assert result == _cotizacion_no_encontrada()
    assert await _pedidos_de_la_quote(quote_id) == 0
    assert await _estado_quote(quote_id) == "aceptada"


async def test_no_aplica_promocion_a_la_cotizacion_de_otro_cliente() -> None:
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    quote_id = await _quote_de(uuid.uuid4(), status="draft")

    async with tenant_scope(T1):
        result = await OrderSkill().aplicar_promocion({"quote_id": quote_id}, _ctx(uuid.uuid4()))

    assert result == _cotizacion_no_encontrada()


async def test_no_toca_el_pedido_de_otro_cliente() -> None:
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    order_id = await _order_de(uuid.uuid4())
    intruso = _ctx(uuid.uuid4())

    async with tenant_scope(T1):
        agregar = await OrderSkill().agregar_item_pedido(
            {"order_id": order_id, "items": [{"variant_id": "X", "quantity": 1}]}, intruso
        )
        pago = await OrderSkill().confirmar_pago_pedido({"order_id": order_id}, intruso)

    assert agregar == {"error": "Pedido no encontrado."}
    assert pago == {"error": "Pedido no encontrado."}


async def test_no_revela_por_numero_el_estado_ni_el_monto_del_pedido_de_otro_cliente() -> None:
    """El número público es secuencial (`OC-0002`): quien conozca el de otro cliente no
    sabe si está pagado ni cuánto vale. Antes lo revelaba `generar_link_pago` de
    pack-payments (`ya_pagado`, `monto_no_coincide` con el monto), que filtraba solo por
    tenant (auditor-multitenant, 2026-10-10); esa tool se retiró (ADR-0087) y por número
    solo queda `consultar_estado_pedido`, que filtra por cliente."""
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    numero = await _numero_de(await _order_de(uuid.uuid4(), payment_status="pagado"))
    intruso = _ctx(uuid.uuid4())

    async with tenant_scope(T1):
        ajeno = await OrderSkill().consultar_estado_pedido({"public_order_number": numero}, intruso)
        inexistente = await OrderSkill().consultar_estado_pedido(
            {"public_order_number": "OC-999999998"}, intruso
        )

    assert ajeno == inexistente
    assert ajeno["found"] is False
    assert "payment_status" not in ajeno and "total" not in ajeno


async def test_el_dueno_si_ve_su_pedido_por_numero() -> None:
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    cliente = uuid.uuid4()
    numero = await _numero_de(await _order_de(cliente, payment_status="pagado"))

    async with tenant_scope(T1):
        propio = await OrderSkill().consultar_estado_pedido({"public_order_number": numero}, _ctx(cliente))

    assert propio["found"] is True, propio
    assert propio["payment_status"] == "pagado"


async def test_la_cotizacion_propia_se_confirma_y_se_convierte_en_pedido() -> None:
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    cliente = uuid.uuid4()
    quote_id = await _quote_de(cliente, status="draft")

    async with tenant_scope(T1):
        confirmada = await OrderSkill().confirmar_cotizacion({"quote_id": quote_id}, _ctx(cliente))
    assert confirmada.get("status") == "aceptada", confirmada

    result = await _crear_pedido(quote_id, _ctx(cliente))

    assert result.get("status") == "confirmed", result
    assert await _pedidos_de_la_quote(quote_id) == 1
    assert await _estado_quote(quote_id) == "convertida"

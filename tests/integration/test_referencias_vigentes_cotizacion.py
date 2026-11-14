"""Referencias vigentes de la conversación con pack-commerce real (ADR-0130).

Incidente (test, tenant solvershub, 2026-10-10): el historial que relee el modelo es
solo texto, así que en el turno siguiente no tenía el `quote_id` real. Llamó
`crear_pedido(quote_id="PLAN-PREMIUM")`, `confirmar_cotizacion("OC-0001")` y
`confirmar_cotizacion("Q-0001")`, generó 6 cotizaciones en 9 minutos y nunca cerró
el pedido.

- La fuente del pack trae solo la cotización en curso y el último pedido de ESTA
  conversación y de ESTE cliente.
- Un `quote_id` que no existe devuelve `quote_id_vigente`, sin usarla en silencio.
- Entre la cotización vigente y el último pedido, el más nuevo es la compra en curso: un
  pedido de otra compra sale como `pedido_anterior`, sin total (incidente del 2026-10-10,
  19:34-19:49: OC-0001 del Basic, pagado, salía como "pedido" junto a la cotización del
  Premium y el modelo dijo "ya figura como pagado, así que no hace falta generar otro").

Los journeys con el loop que cerraban la compra sin procedimiento (`confirmar_cotizacion` y
`crear_pedido` sin tarea en foco) salieron con HU-AOS-69: el motor de procedimientos corre en todo
negocio y ese camino se niega. El recorrido con el motor lo cubre `test_procedimientos_de_comercio`.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PRECIO = 600000.0
PRECIO_BASIC = 200000.0
DATOS = {"full_name": "Ana Gómez", "id_document": "1012345678", "phone": "3001234567", "email": "ana@example.com"}
_QUOTE_EN_PROMPT = re.compile(r"- cotizacion: quote_id=([0-9a-f-]{36})")


# ------------------------------------------------------------------ negocio y datos
async def _negocio(con_basic: bool = False) -> tuple[uuid.UUID, str]:
    """Negocio propio con pack-commerce y un producto «Plan Premium»; devuelve (tenant, variant_id).

    Desde SPEC-43 los planes son de `pack_subscriptions`; aquí «PLAN-PREMIUM» y «PLAN-BASIC» son productos
    normales de commerce (el incidente de ADR-0130 no depende de que fueran planes). Con `con_basic`,
    también el Basic."""
    from pack_commerce.models import Inventory, Product, ProductVariant

    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = f"refs-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"Referencias {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False
    )
    async with tenant_scope(tid):
        async with get_session() as s:
            producto = Product(tenant_id=tid, name="Plan Premium")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(
                tenant_id=tid, product_id=producto.id, sku="PLAN-PREMIUM", price=PRECIO
            )
            s.add(variante)
            await s.flush()
            s.add(Inventory(tenant_id=tid, variant_id=variante.id, stock_quantity=1000))  # ya no son planes: hay stock
            if con_basic:
                basic = Product(tenant_id=tid, name="Plan Basic")
                s.add(basic)
                await s.flush()
                var_basic = ProductVariant(tenant_id=tid, product_id=basic.id, sku="PLAN-BASIC", price=PRECIO_BASIC)
                s.add(var_basic)
                await s.flush()
                s.add(Inventory(tenant_id=tid, variant_id=var_basic.id, stock_quantity=1000))
            await s.flush()
            variante_id = str(variante.id)
    # Todo turno corre sobre la release de su conexión (ADR-0191): el agente nativo trae las acciones de
    # comercio, el pack habilitado del negocio.
    from release_de_prueba import montar_release_en

    await montar_release_en(tid, "whatsapp")
    return tid, variante_id


async def _conversacion(tid: uuid.UUID, customer_id: uuid.UUID | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            if customer_id is None:
                customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"refs:{uuid.uuid4().hex[:8]}")
                s.add(customer)
                await s.flush()
                customer_id = customer.id
            conv = Conversation(tenant_id=tid, customer_id=customer_id, channel="whatsapp")
            s.add(conv)
            await s.flush()
            return conv.id, customer_id


async def _cotizacion(tid: uuid.UUID, conv: uuid.UUID, cliente: uuid.UUID, variant_id: str, status: str = "draft") -> str:
    from pack_commerce.models import Quote, QuoteItem

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            q = Quote(tenant_id=tid, conversation_id=conv, customer_id=cliente,
                      subtotal=PRECIO, discount=0, total=PRECIO, status=status)
            s.add(q)
            await s.flush()
            s.add(QuoteItem(tenant_id=tid, quote_id=q.id, variant_id=uuid.UUID(variant_id), quantity=1, unit_price=PRECIO))
            await s.flush()
            return str(q.id)


def _ctx(conv: uuid.UUID, cliente: uuid.UUID):  # type: ignore[no-untyped-def]
    from core.mcp.types import ToolContext

    return ToolContext(conversation_id=conv, customer_id=cliente,
                       idempotency_key=f"t:{uuid.uuid4().hex[:8]}", channel="whatsapp")


async def _lineas(tid: uuid.UUID, conv: uuid.UUID, cliente: uuid.UUID) -> list[str]:
    from pack_commerce.skills import referencias_vigentes

    from core.orchestrator.referencias import renderizar
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return [renderizar(r) or "" for r in await referencias_vigentes(conv, cliente)]


# ------------------------------------------------------------------ fuente y tools
async def test_solo_la_cotizacion_de_esta_conversacion_y_de_este_cliente() -> None:
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    otra_conv, _ = await _conversacion(tid, cliente)
    _, otro_cliente = await _conversacion(tid)

    propia = await _cotizacion(tid, conv, cliente, variante)
    # Más nuevas, pero de otra conversación del mismo cliente o de otro cliente.
    await _cotizacion(tid, otra_conv, cliente, variante)
    await _cotizacion(tid, conv, otro_cliente, variante)

    assert await _lineas(tid, conv, cliente) == [
        f"- cotizacion: quote_id={propia}, estado=draft, total=600000.00, pedido=ninguno"
    ]


async def test_otro_negocio_no_ve_la_cotizacion() -> None:
    tid, variante = await _negocio()
    otro_tid, _ = await _negocio()
    conv, cliente = await _conversacion(tid)
    await _cotizacion(tid, conv, cliente, variante)

    assert await _lineas(otro_tid, conv, cliente) == []


async def test_una_cotizacion_convertida_ya_no_es_vigente() -> None:
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    await _cotizacion(tid, conv, cliente, variante, status="convertida")

    assert await _lineas(tid, conv, cliente) == []


async def _pedido(
    tid: uuid.UUID, conv: uuid.UUID, cliente: uuid.UUID, *, status: str = "abierto", payment_status: str = "pendiente"
) -> tuple[str, str]:
    """Pedido de la conversación con el número que asigna el pack; devuelve (order_id, número)."""
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            seq = (await s.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tid})).scalar_one()
            order = Order(
                tenant_id=tid, conversation_id=conv, customer_id=cliente, status=status,
                payment_status=payment_status, total=PRECIO_BASIC, idempotency_key=f"refs:{uuid.uuid4().hex}",
                order_seq=seq, public_order_number=f"OC-{seq:04d}",
            )
            s.add(order)
            await s.flush()
            return str(order.id), order.public_order_number


async def test_pedido_pagado_de_otra_compra_sale_como_anterior_sin_total() -> None:
    """El caso del incidente: OC-0001 (Basic, pagado) y después la cotización del Premium."""
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    order_id, numero = await _pedido(tid, conv, cliente, payment_status="pagado")
    quote = await _cotizacion(tid, conv, cliente, variante)

    assert await _lineas(tid, conv, cliente) == [
        f"- cotizacion: quote_id={quote}, estado=draft, total=600000.00, pedido=ninguno",
        f"- pedido_anterior: order_id={order_id}, numero={numero}, estado=abierto, estado_pago=pagado",
    ]


async def test_pedido_pendiente_de_otra_compra_conserva_el_order_id() -> None:
    """Un pedido sin pagar sigue cobrable con `confirmar_pago_pedido` aunque el cliente
    cotice otra cosa: es anterior, pero lleva su `order_id`."""
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    order_id, _ = await _pedido(tid, conv, cliente)
    await _cotizacion(tid, conv, cliente, variante)

    lineas = await _lineas(tid, conv, cliente)
    assert lineas[1].startswith(f"- pedido_anterior: order_id={order_id},")
    assert "estado_pago=pendiente" in lineas[1] and "total=" not in lineas[1]


async def test_una_cotizacion_mas_vieja_que_el_pedido_no_se_expone() -> None:
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    await _cotizacion(tid, conv, cliente, variante)  # draft abandonado
    order_id, numero = await _pedido(tid, conv, cliente)

    assert await _lineas(tid, conv, cliente) == [
        f"- pedido: order_id={order_id}, numero={numero}, estado=abierto, estado_pago=pendiente, total=200000.00"
    ]


@pytest.mark.parametrize("status", ["cancelado", "entregado", "expirado"])
async def test_un_pedido_cerrado_es_anterior_aunque_no_haya_cotizacion(status: str) -> None:
    tid, _ = await _negocio()
    conv, cliente = await _conversacion(tid)
    order_id, numero = await _pedido(tid, conv, cliente, status=status, payment_status="pagado")

    assert await _lineas(tid, conv, cliente) == [
        f"- pedido_anterior: order_id={order_id}, numero={numero}, estado={status}, estado_pago=pagado"
    ]


@pytest.mark.parametrize("quote_id", ["PLAN-PREMIUM", "OC-0001", "Q-0001", str(uuid.uuid5(uuid.NAMESPACE_DNS, "cotizacion-inexistente"))])
async def test_un_id_que_no_existe_devuelve_la_vigente_sin_usarla(quote_id: str) -> None:
    from pack_commerce.models import Quote
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    vigente = await _cotizacion(tid, conv, cliente, variante)

    async with tenant_scope(tid):
        confirmar = await OrderSkill().confirmar_cotizacion({"quote_id": quote_id}, _ctx(conv, cliente))
        pedido = await OrderSkill().crear_pedido(
            {"quote_id": quote_id, "payment_method": "transferencia", "customer_data": DATOS}, _ctx(conv, cliente)
        )
        async with get_session() as s:
            estado = (await s.get(Quote, uuid.UUID(vigente))).status

    for resultado in (confirmar, pedido):
        assert resultado["quote_id_vigente"] == vigente, resultado
        assert resultado["total_vigente"] == PRECIO
        assert "generar_cotizacion" not in json.dumps(resultado, ensure_ascii=False)
    assert estado == "draft", "la vigente no se confirma en silencio"


async def test_sin_cotizacion_vigente_el_error_de_siempre() -> None:
    from pack_commerce.skills import OrderSkill, _sin_cotizacion_vigente

    from core.shared.tenant import tenant_scope

    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    otra_conv, _ = await _conversacion(tid, cliente)
    await _cotizacion(tid, otra_conv, cliente, variante)  # de otra conversación: no cuenta

    async with tenant_scope(tid):
        resultado = await OrderSkill().crear_pedido(
            {"quote_id": "PLAN-PREMIUM", "payment_method": "transferencia", "customer_data": DATOS}, _ctx(conv, cliente)
        )
    # Sin cotización vigente, el error de siempre con la pista de no inventar el quote_id (#414).
    assert resultado == _sin_cotizacion_vigente()
    assert "quote_id_vigente" not in resultado

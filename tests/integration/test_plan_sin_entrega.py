"""Un plan es un servicio: el pedido no pide método de entrega (journeys de HU-SUB-12).

El asistente insistía en "¿domicilio o recoger en tienda?" para el Plan Free y nunca
creaba el pedido; la respuesta además decía "entregado en None, None, None".
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
DATOS = {"full_name": "Ana", "id_document": "1", "phone": "300", "email": "a@example.com"}


async def _cotizacion(precio: float, billing_period: str | None, cliente: uuid.UUID) -> str:
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as s:
            producto = Product(tenant_id=T1, name=f"P {uuid.uuid4().hex[:4]}")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(
                tenant_id=T1, product_id=producto.id, sku=f"S-{uuid.uuid4().hex[:6]}", price=precio,
                billing_period=billing_period,
            )
            s.add(variante)
            await s.flush()
            cotizacion = Quote(tenant_id=T1, conversation_id=uuid.uuid4(), customer_id=cliente,
                               subtotal=precio, discount=0, total=precio, status="aceptada")
            s.add(cotizacion)
            await s.flush()
            s.add(QuoteItem(tenant_id=T1, quote_id=cotizacion.id, variant_id=variante.id, quantity=1, unit_price=precio))
            await s.flush()
            return str(cotizacion.id)


async def _pedido(quote_id: str, cliente: uuid.UUID) -> dict:
    from pack_commerce.skills import OrderSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    ctx = ToolContext(conversation_id=uuid.uuid4(), customer_id=cliente,
                      idempotency_key=f"t:{uuid.uuid4().hex[:8]}", channel="whatsapp")
    async with tenant_scope(T1):
        return await OrderSkill().crear_pedido(
            {"quote_id": quote_id, "payment_method": "efectivo_contraentrega", "customer_data": DATOS}, ctx
        )


async def test_un_plan_se_pide_sin_metodo_de_entrega() -> None:
    cliente = uuid.uuid4()
    resultado = await _pedido(await _cotizacion(0.0, "mensual", cliente), cliente)
    assert resultado.get("status") == "confirmed", resultado
    assert "entregado_en" not in resultado
    assert "delivery_method" not in resultado


async def test_un_producto_sigue_necesitando_metodo_de_entrega() -> None:
    cliente = uuid.uuid4()
    resultado = await _pedido(await _cotizacion(50000.0, None, cliente), cliente)
    assert "método de entrega" in resultado.get("error", "").lower()

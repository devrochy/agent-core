"""`crear_pedido` guarda el perfil por `datos_cliente.guardar` con los `writeNamespaces` del agente.

Hallazgo del auditor multitenant sobre agent-core#816 (ADR-0114 §3, ADR-0179 §10): antes escribía
el perfil directo, sin concesión ni aviso de datos. El pedido se crea igual en todos los casos.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


async def _cliente_con_cotizacion() -> tuple[uuid.UUID, str]:
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem

    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1), get_session() as session:
        cliente = Customer(tenant_id=T1, channel="whatsapp", external_id=f"whatsapp:{uuid.uuid4().hex[:10]}")
        session.add(cliente)
        sku = f"WNS-{uuid.uuid4().hex[:6]}"
        product = Product(tenant_id=T1, name=f"Producto {sku}")
        session.add(product)
        await session.flush()
        variant = ProductVariant(tenant_id=T1, product_id=product.id, sku=sku, price=20_000)
        session.add(variant)
        await session.flush()
        quote = Quote(
            tenant_id=T1, conversation_id=uuid.uuid4(), customer_id=cliente.id,
            subtotal=20_000, discount=0, total=20_000, status="aceptada",
        )
        session.add(quote)
        await session.flush()
        session.add(QuoteItem(tenant_id=T1, quote_id=quote.id, variant_id=variant.id, quantity=1, unit_price=20_000))
        await session.flush()
        return cliente.id, str(quote.id)


async def _pedido(customer_id: uuid.UUID, quote_id: str, concedidos: frozenset[str] | None, consiente: bool):
    from pack_commerce.skills import OrderSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    ctx = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
        write_namespaces=concedidos,
    )
    async with tenant_scope(T1):
        return await OrderSkill().crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "efectivo_contraentrega",
                "delivery_method": "domicilio",
                "guardar_datos_cliente": consiente,
                "customer_data": {
                    "full_name": "Ana Pérez", "id_document": "123", "phone": "3001234567",
                    "address": "Calle 1", "municipality": "Manizales", "city": "Manizales",
                },
            },
            ctx,
        )


async def _perfil(customer_id: uuid.UUID) -> dict:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1), get_session() as session:
        cliente = await session.get(Customer, customer_id)
        assert cliente is not None
        return dict(cliente.attributes or {})


async def test_sin_perfil_concedido_el_pedido_se_crea_y_el_perfil_no_cambia() -> None:
    cliente, quote_id = await _cliente_con_cotizacion()
    res = await _pedido(cliente, quote_id, frozenset(), consiente=True)
    assert res.get("order_id") and "error" not in res, res
    assert await _perfil(cliente) == {}


async def test_con_perfil_concedido_y_consentimiento_guarda_el_perfil() -> None:
    cliente, quote_id = await _cliente_con_cotizacion()
    res = await _pedido(cliente, quote_id, frozenset({"perfil"}), consiente=True)
    assert res.get("order_id") and "error" not in res, res
    perfil = await _perfil(cliente)
    assert perfil == {
        "name": "Ana Pérez", "id_document": "123", "phone": "3001234567", "address": "Calle 1", "municipio": "Manizales",
    }


async def test_sin_consentimiento_no_guarda_nada() -> None:
    cliente, quote_id = await _cliente_con_cotizacion()
    res = await _pedido(cliente, quote_id, frozenset({"perfil"}), consiente=False)
    assert res.get("order_id") and "error" not in res, res
    assert await _perfil(cliente) == {}

"""Datos del alta en el pedido de un plan (HU-SH-03, ADR-0121).

El checkout de un plan guarda en el pedido lo que el alta automática necesita: correo y
teléfono del comprador, nombre de su negocio y su tipo de negocio. El tipo de negocio es un
id de preset de la lista (dato, no texto libre): un valor fuera de la lista se rechaza con
las opciones. La fuente del pack los devuelve por la referencia del pedido.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

DATOS = {
    "full_name": "Ana Pérez",
    "id_document": "1",
    "phone": "+57 300 000 0000",
    "email": "Ana@Correo-Prueba.co",
    "business_name": "Pizzería  Roma",
    "vertical": "restaurante",
}


_NEGOCIO: list[uuid.UUID] = []


async def _negocio() -> uuid.UUID:
    """Negocio propio del módulo: el contador de pedidos no choca con el de otros tests."""
    from core.alta_negocio import onboard_tenant

    if not _NEGOCIO:
        slug = uniq("datos-alta")
        tid, _ = await onboard_tenant(name=f"Negocio {slug}", slug=slug, packs=["pack_commerce"])
        _NEGOCIO.append(tid)
    return _NEGOCIO[0]


async def _cotizacion(precio: float, billing_period: str | None, cliente: uuid.UUID, atributos: dict[str, Any]) -> str:
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        async with get_session() as s:
            producto = Product(tenant_id=tid, name=f"P {uuid.uuid4().hex[:4]}")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(
                tenant_id=tid,
                product_id=producto.id,
                sku=f"S-{uuid.uuid4().hex[:6]}",
                price=precio,
                billing_period=billing_period,
                attributes=atributos,
            )
            s.add(variante)
            await s.flush()
            cotizacion = Quote(
                tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=cliente,
                subtotal=precio, discount=0, total=precio, status="aceptada",
            )
            s.add(cotizacion)
            await s.flush()
            s.add(QuoteItem(tenant_id=tid, quote_id=cotizacion.id, variant_id=variante.id, quantity=1, unit_price=precio))
            await s.flush()
            return str(cotizacion.id)


async def _pedido(quote_id: str, cliente: uuid.UUID, datos: dict[str, Any], **extra: Any) -> dict[str, Any]:
    from pack_commerce.skills import OrderSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    ctx = ToolContext(
        conversation_id=uuid.uuid4(), customer_id=cliente,
        idempotency_key=f"t:{uuid.uuid4().hex[:8]}", channel="whatsapp",
    )
    async with tenant_scope(await _negocio()):
        return await OrderSkill().crear_pedido(
            {"quote_id": quote_id, "payment_method": "transferencia", "customer_data": datos, **extra}, ctx
        )


async def _orden(order_id: str) -> Any:
    from pack_commerce.models import Order

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(await _negocio()):
        async with get_session() as s:
            return (await s.execute(select(Order).where(Order.id == uuid.UUID(order_id)))).scalar_one()


async def test_el_pedido_de_un_plan_guarda_los_datos_del_alta() -> None:
    cliente = uuid.uuid4()
    quote = await _cotizacion(0.0, "mensual", cliente, {"precio_de_plan": "free"})
    resultado = await _pedido(quote, cliente, DATOS)
    assert resultado.get("status") == "confirmed", resultado

    orden = await _orden(resultado["order_id"])
    assert orden.datos_alta == {
        "email": "ana@correo-prueba.co",
        "telefono": "+57 300 000 0000",
        "negocio": "Pizzería Roma",
        "vertical": "restaurante",
    }


def _pide_tipo_de_negocio(monkeypatch: pytest.MonkeyPatch) -> None:
    """El negocio pide nombre y tipo de negocio en el checkout de un plan (su config)."""
    from pack_commerce.config import CRMConfig

    config = CRMConfig(
        checkout_fields={"plan": ["full_name", "phone", "email", "business_name", "vertical"]}
    )

    async def _config() -> CRMConfig:
        return config

    monkeypatch.setattr("pack_commerce.skills._plugin_config", _config)


async def test_un_tipo_de_negocio_fuera_de_la_lista_se_rechaza_con_las_opciones(monkeypatch) -> None:
    from pack_commerce.models import Order

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _pide_tipo_de_negocio(monkeypatch)
    cliente = uuid.uuid4()
    quote = await _cotizacion(0.0, "mensual", cliente, {"precio_de_plan": "free"})
    resultado = await _pedido(quote, cliente, {**DATOS, "vertical": "Comida rápida a domicilio"})

    assert resultado["status"] == "dato_invalido"
    assert resultado["field"] == "vertical"
    assert "restaurante" in {o["id"] for o in resultado["opciones"]}
    async with tenant_scope(await _negocio()):
        async with get_session() as s:
            pedidos = (
                await s.execute(select(Order).where(Order.idempotency_key == f"quote:{quote}"))
            ).scalars().all()
    assert pedidos == []


async def test_si_el_negocio_lo_pide_y_falta_el_tipo_de_negocio_se_pregunta(monkeypatch) -> None:
    _pide_tipo_de_negocio(monkeypatch)
    cliente = uuid.uuid4()
    quote = await _cotizacion(0.0, "mensual", cliente, {"precio_de_plan": "free"})
    datos = {k: v for k, v in DATOS.items() if k != "vertical"}
    resultado = await _pedido(quote, cliente, datos)
    assert resultado["status"] == "faltan_datos_cliente"
    assert resultado["missing_fields"] == ["vertical"]


async def test_si_el_negocio_no_lo_pide_un_tipo_invalido_se_descarta_sin_frenar_el_pedido() -> None:
    """La tool es la misma para todos los negocios: un valor inventado no bloquea una venta."""
    cliente = uuid.uuid4()
    quote = await _cotizacion(0.0, "mensual", cliente, {})
    resultado = await _pedido(quote, cliente, {**DATOS, "vertical": "lo que sea"})
    assert resultado.get("status") == "confirmed", resultado
    datos_alta = (await _orden(resultado["order_id"])).datos_alta
    assert "vertical" not in datos_alta
    assert datos_alta["negocio"] == "Pizzería Roma"


async def test_un_pedido_de_producto_no_guarda_datos_del_alta() -> None:
    cliente = uuid.uuid4()
    quote = await _cotizacion(80_000.0, None, cliente, {})
    datos = {**DATOS, "address": "Calle 1", "municipality": "Centro", "city": "Bogotá"}
    resultado = await _pedido(quote, cliente, datos, delivery_method="domicilio")
    assert resultado.get("status") == "confirmed", resultado
    assert (await _orden(resultado["order_id"])).datos_alta is None


async def test_la_fuente_devuelve_los_datos_del_pedido_por_su_referencia() -> None:
    from pack_commerce.altas import DatosDeAltaDelPedido

    from core.shared.tenant import tenant_scope

    cliente = uuid.uuid4()
    quote = await _cotizacion(0.0, "mensual", cliente, {"precio_de_plan": "free"})
    resultado = await _pedido(quote, cliente, DATOS)
    referencia = resultado["public_order_number"]

    async with tenant_scope(await _negocio()):
        datos = await DatosDeAltaDelPedido().datos_de_alta(referencia)
        nada = await DatosDeAltaDelPedido().datos_de_alta("NO-EXISTE-0001")
    assert datos is not None
    assert (datos["plan"], datos["vertical"], datos["negocio"], datos["email"]) == (
        "free",
        "restaurante",
        "Pizzería Roma",
        "ana@correo-prueba.co",
    )
    assert datos["nombre"] == "Ana Pérez"
    assert datos["pagado"] is True  # Free nace pagado (D-16)
    assert nada is None

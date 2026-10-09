"""Fuente de los datos del alta automática de un negocio (HU-SH-03, ADR-0121).

El core da de alta el negocio del comprador cuando un negocio vendedor (allowlist del core)
activa la suscripción de un plan de la plataforma. Commerce es dueño del pedido: esta fuente
devuelve, por la referencia pública del pedido, el plan de la plataforma de la línea
activada (atributo `precio_de_plan` de la variante, HU-SH-01), los `datos_alta` que guardó
el checkout y si el pedido está pagado. Se registra en `core.provisioning.fuentes`, así el
core no lee tablas del pack ni nombra packs.
"""

from __future__ import annotations

from sqlalchemy import select

from core.db.session import get_session
from core.provisioning.fuentes import DatosDeAlta
from core.shared.tenant import get_current_tenant_id
from pack_commerce.models import Order
from pack_commerce.planes import _lineas_de_plan
from pack_commerce.precios_de_plan import ATRIBUTO


class DatosDeAltaDelPedido:
    provider_id = "pack_commerce"

    async def datos_de_alta(
        self, referencia: str, *, variant_id: str | None = None
    ) -> DatosDeAlta | None:
        tenant_id = get_current_tenant_id()
        referencia = (referencia or "").strip()
        if tenant_id is None or not referencia:
            return None
        async with get_session() as session:
            order = (
                await session.execute(
                    select(Order).where(
                        Order.tenant_id == tenant_id,
                        Order.public_order_number == referencia,
                    )
                )
            ).scalar_one_or_none()
            if order is None:
                return None
            lineas = await _lineas_de_plan(session, order)
        variantes = [variant for _item, variant in lineas]
        elegida = next((v for v in variantes if variant_id and str(v.id) == variant_id), None)
        if elegida is None and len(variantes) == 1:
            elegida = variantes[0]
        atributos = (elegida.attributes or {}) if elegida else {}
        plan = str(atributos.get(ATRIBUTO) or "").strip().lower()
        datos = dict(order.datos_alta or {})
        return {
            "plan": plan or None,
            "vertical": datos.get("vertical"),
            "negocio": datos.get("negocio"),
            "email": datos.get("email"),
            "telefono": datos.get("telefono"),
            "nombre": order.delivery_full_name,
            "customer_id": str(order.customer_id) if order.customer_id else None,
            # Un plan va en su propio pedido (agregar_item_pedido no los mezcla): un
            # pedido de plan "pagado" es un Free confirmado o un pago real (D-16).
            "pagado": order.payment_status == "pagado",
        }

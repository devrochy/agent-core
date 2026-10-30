"""Procedimientos de comercio (HU-AOS-07, ADR-0113 §1): resolvers y resumen de confirmación.

Los procedimientos son datos del pack (`procedures/*.json`, invariante 3): `tomar_pedido`
(pedido con entrega). Contratar un plan es del pack de Suscripciones (SPEC-43). Solo los ejecuta el
motor de procedimientos del core (`tenants.aos_procedures`); con la bandera apagada nada de esto
corre.

- **Resolvers** (reciben solo `ToolContext`, sin efectos): convierten lo que dice el cliente en un
  dato válido del servidor o devuelven un error de negocio que el modelo corrige.
  - `metodo_de_pago`: `pago_en_linea` solo si el negocio tiene pagos en línea.
- **Resumen de `confirmar_cotizacion`**: lo que el cliente confirma sale de la cotización
  vigente en la base (productos, descuento y total), no de los argumentos ni del modelo; el kernel
  le suma los datos del cliente de la tarea (resumen único).
- **Rótulos** de los datos (`rotulos_de_datos.json`): los nombres que ve el cliente en los
  mensajes que arma el kernel ("Correo" en vez de `email`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import DatoDeResumen, ResolverResult, ToolContext
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_commerce.models import Product, ProductVariant, QuoteItem

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_commerce"


def registrar(host: MCPHost) -> None:
    """Procedimientos, resolvers y resumen del pack (se llama en `on_activate`)."""
    from core.kernel.procedures import de_pack

    host.register_procedures(de_pack.cargar(PLUGIN_ID))
    host.register_resolver(f"{PLUGIN_ID}.metodo_de_pago", validar=metodo_de_pago)
    host.register_resumen("confirmar_cotizacion", resumen_de_cotizacion)
    host.register_rotulos(rotulos_de_datos())


@cache
def rotulos_de_datos() -> Mapping[str, str]:
    """Rótulos humanos de los datos de los procedimientos del pack (dato del pack, no código)."""
    crudo = json.loads((Path(__file__).resolve().parent / "rotulos_de_datos.json").read_text(encoding="utf-8"))
    return MappingProxyType({str(k): str(v) for k, v in crudo.items()})


# ------------------------------------------------------------------ resolvers
async def _pago_en_linea_disponible() -> bool:
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import payment_registry

    spec = await load_active_spec(get_current_tenant_id())
    return payment_registry.capability_for(enabled_packs(spec)) is not None


async def metodo_de_pago(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """`pago_en_linea` solo si el negocio cobra en línea: sin eso la tarea llegaría a pagar sin
    poder emitir el link (y tras crear el pedido ya no se cambia)."""
    if valor != "pago_en_linea":
        return ResolverResult(ok=True)
    if not await _pago_en_linea_disponible():
        return ResolverResult(
            ok=False, error="El pago en línea no está disponible en este negocio: ofrece otro método de pago."
        )
    return ResolverResult(ok=True)


# ------------------------------------------------------------------ resumen de la confirmación
async def resumen_de_cotizacion(args: dict[str, Any], ctx: ToolContext) -> list[DatoDeResumen] | None:
    """Lo que el cliente confirma con `confirmar_cotizacion`: la cotización VIGENTE del cliente
    (productos y cantidades; descuento y total). None si no es suya o ya no está en curso: el kernel manda el texto fijo sin datos ni botón."""
    from pack_commerce.skills import QUOTE_STATUS_VIGENTE, _float, _quote_del_cliente

    try:
        quote_id = uuid.UUID(str(args.get("quote_id") or ""))
    except ValueError:
        return None
    async with get_session() as session:
        quote = await _quote_del_cliente(session, quote_id, ctx.customer_id)
        if quote is None or quote.status not in QUOTE_STATUS_VIGENTE:
            return None
        lineas = (
            await session.execute(
                select(QuoteItem, ProductVariant, Product)
                .join(ProductVariant, ProductVariant.id == QuoteItem.variant_id)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(
                    QuoteItem.tenant_id == get_current_tenant_id(),
                    QuoteItem.quote_id == quote.id,
                    ProductVariant.tenant_id == get_current_tenant_id(),
                    Product.tenant_id == get_current_tenant_id(),
                )
                .order_by(Product.name, ProductVariant.sku)
            )
        ).all()
        descuento, total = _float(quote.discount), _float(quote.total)
    if not lineas:
        return None
    # Lo que se compra y después los montos (el kernel no los recorta).
    datos: list[DatoDeResumen] = []
    vistos: set[str] = set()
    for item, variant, product in lineas:
        nombre = product.name if product.name not in vistos else f"{product.name} {variant.sku}"
        vistos.add(nombre)
        datos.append(DatoDeResumen(nombre, int(item.quantity)))
    if descuento > 0:
        datos.append(DatoDeResumen("descuento", descuento, es_monto=True))
    datos.append(DatoDeResumen("total", total, es_monto=True))
    if len(datos) > 20:
        return None  # sin recortar: el cliente confirma todo lo que ve
    return datos

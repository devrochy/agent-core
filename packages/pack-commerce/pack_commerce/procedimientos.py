"""Procedimientos de comercio (HU-AOS-07, ADR-0113 §1): resolvers y resumen de confirmación.

Los procedimientos son datos del pack (`procedures/*.json`, invariante 3): `contratar_plan` y
`tomar_pedido` (pedido con entrega). Solo los ejecuta el motor de procedimientos del core
(`tenants.aos_procedures`); con la bandera apagada nada de esto corre.

- **Resolvers** (reciben solo `ToolContext`, sin efectos): convierten lo que dice el cliente en un
  dato válido del servidor o devuelven un error de negocio que el modelo corrige.
  - `variante_de_plan`: "el premium mensual", el SKU o el uuid → el `variant_id` de UN plan activo.
    Reusa el catálogo de commerce (el mismo que `CatalogoDePlanes`, ADR-0111), no lo duplica.
  - `metodo_de_pago`: `pago_en_linea` solo si el negocio tiene pagos en línea y el plan se cobra.
  - `tipo_de_negocio` y `nombre_de_negocio`: las mismas reglas de `crear_pedido` (ADR-0121/0129),
    antes de confirmar en vez de después.
- **Resumen de `confirmar_cotizacion`**: lo que el cliente confirma sale de la cotización
  vigente en la base (plan o productos, descuento y total), no de los argumentos ni del modelo.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import DatoDeResumen, ResolverResult, ToolContext
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.shared.texto_seguro import contiene_url
from pack_commerce.models import Product, ProductVariant, QuoteItem

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_commerce"
# Palabras que no distinguen un plan de otro ("quiero el plan premium").
_RELLENO = frozenset({"plan", "planes", "el", "la", "de", "del", "quiero", "por", "favor", "con"})
_MAX_OPCIONES = 6
_MAX_PLANES = 50


def registrar(host: MCPHost) -> None:
    """Procedimientos, resolvers y resumen del pack (se llama en `on_activate`)."""
    from core.kernel.procedures import de_pack

    host.register_procedures(de_pack.cargar(PLUGIN_ID))
    host.register_resolver(f"{PLUGIN_ID}.variante_de_plan", validar=variante_de_plan)
    host.register_resolver(f"{PLUGIN_ID}.metodo_de_pago", validar=metodo_de_pago)
    host.register_resolver(f"{PLUGIN_ID}.tipo_de_negocio", validar=tipo_de_negocio)
    host.register_resolver(f"{PLUGIN_ID}.nombre_de_negocio", validar=nombre_de_negocio)
    host.register_resumen("confirmar_cotizacion", resumen_de_cotizacion)


# ------------------------------------------------------------------ resolvers
def _normalizar(texto: str) -> str:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", texto or "") if unicodedata.category(c) != "Mn"
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes.lower()).split())


def _opcion(product: Product, variant: ProductVariant) -> str:
    """Una opción como dato: el nombre y el SKU van entre comillas y escapados (invariante 7),
    así el texto del catálogo no se lee como una instrucción."""
    from core.kernel.tarjeta import escapar

    return f"{escapar(product.name)} (sku {escapar(variant.sku)}, {escapar(variant.billing_period)})"


async def variante_de_plan(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """El `variant_id` de UN plan activo: por uuid, por SKU exacto o por las palabras del cliente
    (todas tienen que aparecer en el nombre, el SKU o la periodicidad de un solo plan)."""
    from pack_commerce.skills import _plan_por_referencia

    texto = str(valor or "").strip()[:200]
    exacta = await _plan_por_referencia(texto)
    if exacta is not None:
        return ResolverResult(ok=True, value=str(exacta[0].id))
    async with get_session() as session:
        filas = (
            await session.execute(
                select(ProductVariant, Product)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(
                    ProductVariant.tenant_id == get_current_tenant_id(),
                    Product.tenant_id == get_current_tenant_id(),
                    ProductVariant.active.is_(True),
                    ProductVariant.billing_period.isnot(None),
                )
                .order_by(Product.name, ProductVariant.sku)
                .limit(_MAX_PLANES)
            )
        ).all()
    if not filas:
        return ResolverResult(ok=False, error="Este negocio no tiene planes en su catálogo.")
    palabras = [p for p in _normalizar(texto).split() if p not in _RELLENO]
    coinciden = [
        (v, p)
        for v, p in filas
        if palabras and all(w in _normalizar(f"{p.name} {v.sku} {v.billing_period}").split() for w in palabras)
    ]
    if len(coinciden) == 1:
        return ResolverResult(ok=True, value=str(coinciden[0][0].id))
    candidatas = coinciden or filas
    opciones = "; ".join(_opcion(p, v) for v, p in candidatas[:_MAX_OPCIONES])
    if coinciden:
        return ResolverResult(ok=False, error=f"Hay varios planes que coinciden: {opciones}. Pregúntale cuál.")
    return ResolverResult(ok=False, error=f"No encontré ese plan. Planes disponibles: {opciones}.")


async def _pago_en_linea_disponible() -> bool:
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import payment_registry

    spec = await load_active_spec(get_current_tenant_id())
    return payment_registry.capability_for(enabled_packs(spec)) is not None


async def metodo_de_pago(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """`pago_en_linea` solo si el negocio cobra en línea y el plan tiene costo: sin eso la tarea
    llegaría a pagar sin poder emitir el link (y tras crear el pedido ya no se cambia)."""
    if valor != "pago_en_linea":
        return ResolverResult(ok=True)
    if datos.get("requiere_pago") is False:
        return ResolverResult(ok=False, error="Este plan no tiene costo: no hace falta pagar en línea.")
    if not await _pago_en_linea_disponible():
        return ResolverResult(
            ok=False, error="El pago en línea no está disponible en este negocio: ofrece otro método de pago."
        )
    return ResolverResult(ok=True)


async def tipo_de_negocio(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """El tipo de negocio es el id de un preset (ADR-0121), como en `crear_pedido`."""
    from pack_commerce.skills import _vertical_invalido

    rechazo = _vertical_invalido(valor)
    if rechazo is None:
        return ResolverResult(ok=True)
    ids = ", ".join(o["id"] for o in rechazo.get("opciones") or [])
    return ResolverResult(ok=False, error=f"Tipo de negocio no válido. Opciones: {ids}.")


async def nombre_de_negocio(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """Sin enlaces ni dominios (va en correos y avisos del alta), como en `crear_pedido`."""
    if contiene_url(str(valor or "")):
        return ResolverResult(ok=False, error="El nombre del negocio no puede llevar enlaces ni dominios.")
    return ResolverResult(ok=True, value=" ".join(str(valor).split())[:120])


# ------------------------------------------------------------------ resumen de la confirmación
async def resumen_de_cotizacion(args: dict[str, Any], ctx: ToolContext) -> list[DatoDeResumen] | None:
    """Lo que el cliente confirma con `confirmar_cotizacion`: la cotización VIGENTE del cliente
    (plan y periodicidad, o productos y cantidades; descuento y total). None si no es suya o ya no
    está en curso: el kernel manda el texto fijo sin datos ni botón."""
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
    # Los montos primero: si el resumen se recorta por largo, nunca se pierde el total.
    datos: list[DatoDeResumen] = []
    if descuento > 0:
        datos.append(DatoDeResumen("descuento", descuento, es_monto=True))
    datos.append(DatoDeResumen("total", total, es_monto=True))
    if len(lineas) == 1 and lineas[0][1].billing_period and lineas[0][0].quantity == 1:
        _item, variant, product = lineas[0]
        datos += [DatoDeResumen("plan", product.name), DatoDeResumen("periodicidad", variant.billing_period)]
    else:
        vistos: set[str] = set()
        for item, variant, product in lineas:
            nombre = product.name if product.name not in vistos else f"{product.name} {variant.sku}"
            vistos.add(nombre)
            datos.append(DatoDeResumen(nombre, int(item.quantity)))
    return datos[:20]

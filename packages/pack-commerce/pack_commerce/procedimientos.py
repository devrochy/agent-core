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
    antes de confirmar en vez de después. El tipo acepta cómo lo llama el cliente ("tienda",
    "pizzería": los `sinonimos` de cada preset) y, si no lo reconoce, ofrece las opciones con su
    nombre y descripción (adenda 2026-10-13 de ADR-0113).
- **Resumen de `confirmar_cotizacion`**: lo que el cliente confirma sale de la cotización
  vigente en la base (plan o productos, descuento y total), no de los argumentos ni del modelo. En
  un vendedor de planes de la plataforma lleva además los términos, la privacidad y el DPA que el
  cliente acepta con el mismo «Sí, confirmo» (adenda 2026-10-13 de ADR-0125); el kernel le suma los
  datos del cliente de la tarea (resumen único).
- **Rótulos** de los datos (`rotulos_de_datos.json`): los nombres que ve el cliente en los
  mensajes que arma el kernel ("Correo" en vez de `email`).
"""

from __future__ import annotations

import json
import re
import unicodedata
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
    host.register_rotulos(rotulos_de_datos())


@cache
def rotulos_de_datos() -> Mapping[str, str]:
    """Rótulos humanos de los datos de los procedimientos del pack (dato del pack, no código)."""
    crudo = json.loads((Path(__file__).resolve().parent / "rotulos_de_datos.json").read_text(encoding="utf-8"))
    return MappingProxyType({str(k): str(v) for k, v in crudo.items()})


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
                    Product.deleted_at.is_(None),
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


def _presets_con_sinonimos() -> list[tuple[str, str, str, list[str]]]:
    """(id, nombre, descripción, sinónimos) de los presets del core (ADR-0110); [] si no cargan."""
    from core.presets import PresetError, list_presets

    try:
        return [(p.id, p.nombre, p.descripcion, list(p.sinonimos)) for p in list_presets()]
    except PresetError:
        log.warning("tipos_de_negocio_no_disponibles", exc_info=True)
        return []


def tipo_desde_texto(texto: str, presets: list[tuple[str, str, str, list[str]]]) -> str | None:
    """El id del ÚNICO preset que nombra el texto del cliente: su id, su nombre o uno de sus
    sinónimos, como frase completa dentro del texto ("una tienda de ropa" → comercio). None si no
    nombra ninguno o nombra más de uno. Pura."""
    norm = f" {_normalizar(texto)} "
    if not norm.strip():
        return None
    encontrados = set()
    for pid, nombre, _desc, sinonimos in presets:
        for frase in (pid, nombre, *sinonimos):
            f = _normalizar(frase)
            if f and f" {f} " in norm:
                encontrados.add(pid)
                break
    return encontrados.pop() if len(encontrados) == 1 else None


async def tipo_de_negocio(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """El tipo de negocio es el id de un preset (ADR-0121), como en `crear_pedido`. Acepta cómo lo
    dice el cliente (`sinonimos` del preset): "tienda" → `comercio`. Si no lo reconoce, el error
    trae las opciones con su nombre y descripción (escapados como dato) para preguntarle al
    cliente con sus palabras (nombre y ejemplos de cada preset), no una lista de ids."""
    from core.kernel.tarjeta import escapar

    presets = _presets_con_sinonimos()
    texto = str(valor or "").strip()[:200]
    if texto in {p[0] for p in presets}:
        return ResolverResult(ok=True, value=texto)
    elegido = tipo_desde_texto(texto, presets)
    if elegido is not None:
        return ResolverResult(ok=True, value=elegido)
    # El error del resolver se acota a 300 caracteres (`textos.error_de_resolver`): nombre y dos
    # ejemplos de cada opción, como dato.
    opciones = "; ".join(
        f"{escapar(nombre)} ({escapar(', '.join(sin[:2]))})" if sin else escapar(nombre)
        for _pid, nombre, _desc, sin in presets
    )
    return ResolverResult(
        ok=False,
        error=f"No reconocí el tipo de negocio. Pregúntale cuál se parece más al suyo: {opciones}.",
    )


async def nombre_de_negocio(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """Sin enlaces ni dominios (va en correos y avisos del alta), como en `crear_pedido`."""
    if contiene_url(str(valor or "")):
        return ResolverResult(ok=False, error="El nombre del negocio no puede llevar enlaces ni dominios.")
    return ResolverResult(ok=True, value=" ".join(str(valor).split())[:120])


# ------------------------------------------------------------------ resumen de la confirmación
def _nombre_de_plan(nombre: str, periodicidad: str | None) -> str:
    """"Plan Premium" + "mensual" → "Premium mensual": el rótulo ya dice que es un plan."""
    limpio = re.sub(r"^\s*plan\s+", "", nombre or "", flags=re.IGNORECASE).strip() or (nombre or "").strip()
    return f"{limpio} {periodicidad}".strip() if periodicidad else limpio

async def resumen_de_cotizacion(args: dict[str, Any], ctx: ToolContext) -> list[DatoDeResumen] | None:
    """Lo que el cliente confirma con `confirmar_cotizacion`: la cotización VIGENTE del cliente
    (el plan con su periodicidad, o productos y cantidades; descuento y total) y, en el pedido de
    un plan de un vendedor de la plataforma, los documentos que acepta. None si no es suya o ya no
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
    # Lo que se compra, después los montos (el kernel no los recorta) y al final los documentos
    # que se aceptan (adenda 2026-10-13).
    datos: list[DatoDeResumen] = []
    if len(lineas) == 1 and lineas[0][1].billing_period and lineas[0][0].quantity == 1:
        _item, variant, product = lineas[0]
        datos.append(DatoDeResumen("plan", _nombre_de_plan(product.name, variant.billing_period)))
    else:
        vistos: set[str] = set()
        for item, variant, product in lineas:
            nombre = product.name if product.name not in vistos else f"{product.name} {variant.sku}"
            vistos.add(nombre)
            datos.append(DatoDeResumen(nombre, int(item.quantity)))
    if descuento > 0:
        datos.append(DatoDeResumen("descuento", descuento, es_monto=True))
    datos.append(DatoDeResumen("total", total, es_monto=True))
    if any(variant.billing_period for _item, variant, _product in lineas):
        # Pedido de un plan en un vendedor de la plataforma (ADR-0125): los documentos se aceptan
        # con el mismo «Sí, confirmo» del resumen. Anota el marcador fijo por versión antes de que
        # salga el mensaje que los lleva (SET NX: armar el resumen otra vez no lo mueve).
        from core.provisioning.antiabuso import terminos_del_resumen

        terminos = await terminos_del_resumen(ctx.conversation_id, anotar=True)
        if terminos is not None:
            datos += [
                DatoDeResumen(str(d.get("nombre") or ""), str(d.get("url") or ""), documento_aceptado=True)
                for d in terminos.documentos
            ]
            # La versión de cada documento entra en la huella sin mostrarse (un nombre `*_id` no se
            # muestra): otra versión con el mismo enlace pide otro sí.
            versiones = ",".join(f"{d.get('tipo')}:{d.get('version')}" for d in terminos.documentos)
            datos.append(DatoDeResumen("documentos_version_id", versiones))
    if len(datos) > 20:
        return None  # sin recortar: el cliente confirma todo lo que ve
    return datos

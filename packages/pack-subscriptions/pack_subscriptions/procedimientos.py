"""Procedimientos `pack_subscriptions.cambiar_plan` (HU-AOS-08) y `pack_subscriptions.contratar_plan`
(SPEC-43, HU-SUB-29/34), con sus *resolvers* y el resumen de la inscripción (ADR-0113 §1).

`contratar_plan`: el cliente elige un plan, da sus datos una sola vez, ve UN resumen del servidor (plan,
descuento, total del primer cargo, desde el siguiente período y, en un vendedor de la plataforma, los
documentos que acepta con el mismo «Sí, confirmo») y paga. El resumen sale de la base, no de los argumentos
del modelo; el LLM no decide precio, periodicidad ni cliente.

El JSON vive en `packages/pack-subscriptions/procedures/`. Las acciones son del core (ADR-0106:
un solo juego de tools `sub_*`): `sub_consultar`, `sub_cotizar_cambio_plan` y
`sub_aplicar_cambio_plan` (adenda de ADR-0111). El precio y el prorrateo salen del catálogo del
servidor (ADR-0111) y el monto que el cliente confirma con el botón se compara en el servidor con
el que se cobra.

`plan_destino` valida el plan que escribe el modelo con la misma cotización que la tool: el plan
existe en el catálogo del negocio y el cambio es posible (no es el mismo plan, la suscripción está
activa, misma periodicidad…). Si no, devuelve el rechazo del dominio en lenguaje claro.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.mcp.types import DatoDeResumen, ResolverResult, ToolContext
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.texto_seguro import contiene_url
from core.subscriptions import SubscriptionError

log = get_logger(__name__)

# Palabras que no distinguen un plan de otro ("quiero el plan premium").
_RELLENO = frozenset({"plan", "planes", "el", "la", "de", "del", "quiero", "por", "favor", "con"})
_MAX_OPCIONES = 6


async def plan_destino(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    from core.subscriptions.tools import cotizacion_del_cliente

    try:
        cotizacion = await cotizacion_del_cliente(ctx, str(valor or ""))
    except SubscriptionError as exc:
        return ResolverResult(ok=False, error=exc.message)
    return ResolverResult(ok=True, value=cotizacion["plan_code"])


def registrar(host: Any, plugin_id: str) -> None:
    """Publica los procedimientos, sus resolvers, el resumen y los rótulos (lo llama `on_activate`). Sin banderas
    encendidas nadie los usa: el motor solo actúa con `tenants.aos_procedures` (o en sombra con `aos_runtime = v3`)."""
    from core.kernel.procedures import de_pack

    host.register_resolver(f"{plugin_id}.plan_destino", validar=plan_destino)
    host.register_resolver(f"{plugin_id}.plan_a_contratar", validar=plan_a_contratar)
    host.register_resolver(f"{plugin_id}.codigo_de_promocion", validar=codigo_de_promocion)
    host.register_resolver(f"{plugin_id}.metodo_de_pago", validar=metodo_de_pago)
    host.register_resolver(f"{plugin_id}.tipo_de_negocio", validar=tipo_de_negocio)
    host.register_resolver(f"{plugin_id}.nombre_de_negocio", validar=nombre_de_negocio)
    host.register_procedures(de_pack.cargar(plugin_id))
    try:
        host.register_resumen("sub_confirmar_inscripcion", resumen_de_inscripcion)
    except PluginError:
        # La tool es del core: si sus skills aún no están registradas, el resumen cae al texto fijo del kernel;
        # no se mata la activación del pack por eso.
        log.warning("subs_resumen_de_inscripcion_no_registrado", exc_info=True)
    host.register_rotulos(rotulos_de_datos())


@cache
def rotulos_de_datos() -> Mapping[str, str]:
    """Rótulos humanos de los datos de los procedimientos del pack (dato del pack, no código)."""
    crudo = json.loads((Path(__file__).resolve().parent / "rotulos_de_datos.json").read_text(encoding="utf-8"))
    return MappingProxyType({str(k): str(v) for k, v in crudo.items()})


# ------------------------------------------------------------------ resolvers de contratar_plan
def _normalizar(texto: str) -> str:
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", texto or "") if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes.lower()).split())


def _opcion(plan: Mapping[str, Any]) -> str:
    """Una opción como dato: el nombre y el SKU van entre comillas y escapados (invariante 7)."""
    from core.kernel.tarjeta import escapar

    return f"{escapar(str(plan.get('nombre') or ''))} (sku {escapar(str(plan.get('plan_code') or ''))}, {escapar(str(plan.get('periodicidad') or ''))})"


async def plan_a_contratar(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """El `plan_code` de UN plan activo: por SKU exacto o por las palabras del cliente (todas tienen que aparecer en
    el nombre, el SKU o la periodicidad de un solo plan)."""
    from core.subscriptions.tools import _inscripcion

    try:
        cap = await _inscripcion(ctx)
        texto = str(valor or "").strip()[:200]
        exacto = await cap.plan_por_codigo(texto) if texto else None
        if exacto is not None:
            return ResolverResult(ok=True, value=str(exacto.get("plan_code")))
        planes = await cap.listar_planes(solo_activos=True)
    except SubscriptionError as exc:
        return ResolverResult(ok=False, error=exc.message)
    if not planes:
        return ResolverResult(ok=False, error="Este negocio no tiene planes en su catálogo.")
    palabras = [p for p in _normalizar(texto).split() if p not in _RELLENO]
    coinciden = [
        p
        for p in planes
        if palabras
        and all(
            w in _normalizar(f"{p.get('nombre')} {p.get('plan_code')} {p.get('periodicidad')}").split() for w in palabras
        )
    ]
    if len(coinciden) == 1:
        return ResolverResult(ok=True, value=str(coinciden[0].get("plan_code")))
    candidatos = coinciden or planes
    opciones = "; ".join(_opcion(p) for p in candidatos[:_MAX_OPCIONES])
    if coinciden:
        return ResolverResult(ok=False, error=f"Hay varios planes que coinciden: {opciones}. Pregúntale cuál.")
    return ResolverResult(ok=False, error=f"No encontré ese plan. Planes disponibles: {opciones}.")


async def codigo_de_promocion(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """El código de promoción se valida con la misma cotización que la tool (vigencia, plan, cupo): un código que no
    aplica responde un mensaje único, sin decir por qué (no se puede adivinar códigos)."""
    from core.subscriptions.tools import cotizacion_de_inscripcion

    texto = str(valor or "").strip()[:80]
    if not texto:
        return ResolverResult(ok=True)
    plan = str(datos.get("plan_code") or "")
    if not plan:
        return ResolverResult(ok=False, error="Primero hay que elegir el plan para aplicar el código.")
    try:
        await cotizacion_de_inscripcion(ctx, plan, texto)
    except SubscriptionError as exc:
        return ResolverResult(ok=False, error=exc.message)
    return ResolverResult(ok=True, value=texto)


async def _pago_en_linea_disponible() -> bool:
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import payment_registry
    from core.shared.tenant import get_current_tenant_id

    spec = await load_active_spec(get_current_tenant_id())
    return payment_registry.capability_for(enabled_packs(spec)) is not None


async def metodo_de_pago(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """`pago_en_linea` solo si el negocio cobra en línea y el plan tiene costo."""
    if valor != "pago_en_linea":
        return ResolverResult(ok=True)
    if datos.get("requiere_pago") is False:
        return ResolverResult(ok=False, error="Este plan no tiene costo: no hace falta pagar en línea.")
    if not await _pago_en_linea_disponible():
        return ResolverResult(
            ok=False, error="El pago en línea no está disponible en este negocio: ofrece transferencia."
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
    """El id del ÚNICO preset que nombra el texto del cliente (su id, nombre o un sinónimo, como frase completa);
    None si no nombra ninguno o nombra más de uno. Pura."""
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
    """El tipo de negocio es el id de un preset (ADR-0121). Acepta cómo lo dice el cliente ("tienda" → comercio);
    si no lo reconoce, el error trae las opciones (escapadas como dato) para preguntarle con sus palabras."""
    from core.kernel.tarjeta import escapar

    presets = _presets_con_sinonimos()
    texto = str(valor or "").strip()[:200]
    if texto in {p[0] for p in presets}:
        return ResolverResult(ok=True, value=texto)
    elegido = tipo_desde_texto(texto, presets)
    if elegido is not None:
        return ResolverResult(ok=True, value=elegido)
    opciones = "; ".join(
        f"{escapar(nombre)} ({escapar(', '.join(sin[:2]))})" if sin else escapar(nombre)
        for _pid, nombre, _desc, sin in presets
    )
    return ResolverResult(
        ok=False, error=f"No reconocí el tipo de negocio. Pregúntale cuál se parece más al suyo: {opciones}."
    )


async def nombre_de_negocio(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """Sin enlaces ni dominios (va en correos y avisos del alta)."""
    if contiene_url(str(valor or "")):
        return ResolverResult(ok=False, error="El nombre del negocio no puede llevar enlaces ni dominios.")
    return ResolverResult(ok=True, value=" ".join(str(valor).split())[:120])


# ------------------------------------------------------------------ resumen de la confirmación
def _nombre_de_plan(nombre: str, periodicidad: str | None) -> str:
    """"Plan Premium" + "mensual" → "Premium mensual": el rótulo ya dice que es un plan."""
    limpio = re.sub(r"^\s*plan\s+", "", nombre or "", flags=re.IGNORECASE).strip() or (nombre or "").strip()
    return f"{limpio} {periodicidad}".strip() if periodicidad else limpio


async def resumen_de_inscripcion(args: dict[str, Any], ctx: ToolContext) -> list[DatoDeResumen] | None:
    """Lo que el cliente confirma con `sub_confirmar_inscripcion`: la cotización VIGENTE del servidor (plan con su
    periodicidad, descuento, total del primer cargo y precio desde el siguiente período) y, en un vendedor de la
    plataforma, los documentos que acepta con el mismo «Sí, confirmo». None si el plan o el código ya no valen: el
    kernel manda el texto fijo sin datos ni botón."""
    from core.subscriptions.tools import cotizacion_de_inscripcion

    try:
        cot = await cotizacion_de_inscripcion(ctx, str(args.get("plan_code") or ""), args.get("codigo_promocion"))
    except SubscriptionError:
        return None
    datos: list[DatoDeResumen] = [DatoDeResumen("plan", _nombre_de_plan(str(cot.get("plan") or ""), cot.get("periodicidad")))]
    descuento = float(cot.get("descuento") or 0)
    if descuento > 0:
        etiqueta = str(cot.get("promocion") or "").strip()
        datos.append(DatoDeResumen(f"descuento ({etiqueta})" if etiqueta else "descuento", descuento, es_monto=True))
    datos.append(DatoDeResumen("total del primer cargo", float(cot["total_primer_cargo"]), es_monto=True))
    if descuento > 0 and float(cot.get("precio_periodo_siguiente") or 0) != float(cot["total_primer_cargo"]):
        datos.append(
            DatoDeResumen("desde el siguiente período", float(cot["precio_periodo_siguiente"]), es_monto=True)
        )
    from core.provisioning.antiabuso import terminos_del_resumen

    terminos = await terminos_del_resumen(ctx.conversation_id, anotar=True)
    if terminos is not None:
        datos += [
            DatoDeResumen(str(d.get("nombre") or ""), str(d.get("url") or ""), documento_aceptado=True)
            for d in terminos.documentos
        ]
        versiones = ",".join(f"{d.get('tipo')}:{d.get('version')}" for d in terminos.documentos)
        datos.append(DatoDeResumen("documentos_version_id", versiones))
    return datos if len(datos) <= 20 else None

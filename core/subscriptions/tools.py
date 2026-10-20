"""Tools del core para suscripciones (HU-SUB-01, ADR-0106).

Un solo juego de tools para el LLM — `sub_consultar`, `sub_link_pago`,
`sub_pausar`, `sub_cancelar`, `sub_cambiar_plan` — definido en el core y
despachado al proveedor del negocio (`subscription_registry`). Se ofrecen solo
cuando el tenant tiene un proveedor de suscripciones habilitado (predicado por
request): los demás negocios nunca las ven.

Las tools son plomería de despacho: no tocan modelos ni pasarelas. El error de
negocio del proveedor (`SubscriptionError`) ya sale como resultado controlado.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, ClassVar

from core.kernel.oferta import sin_motor_activo, solo_con_motor_activo
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.subscriptions import (
    PlanSnapshot,
    SubscriptionCapability,
    SubscriptionError,
    SubscriptionView,
    capability_del_negocio,
    cotizar_cambio,
    soporta_cotizacion,
    subscription_registry,
)

log = get_logger(__name__)


async def _capability(ctx: ToolContext) -> SubscriptionCapability:
    """Proveedor del negocio de la conversación (fail-closed)."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise SubscriptionError("Sin negocio en contexto para suscripciones.", code="sin_negocio")
    cap = await capability_del_negocio(tenant_id)
    if cap is None:
        raise SubscriptionError(
            "Este negocio no tiene un proveedor de suscripciones habilitado.",
            code="sin_proveedor",
        )
    return cap


async def _suscripcion_del_cliente(ctx: ToolContext) -> SubscriptionView | None:
    cap = await _capability(ctx)
    suscripciones = await cap.list_for_customer(ctx.customer_id)
    activas = [s for s in suscripciones if s.get("estado") != "cancelada"]
    return activas[0] if activas else None


def _proveedor_habilitado() -> Callable[[set[str]], Awaitable[bool]]:
    """Predicado de oferta: solo tenants con un proveedor de suscripciones activo.

    Los proveedores se leen del registro EN CADA REQUEST, no al registrar la skill:
    las skills del core se registran antes de que los packs se activen (al importar
    `core.main`, antes del lifespan), y un conjunto calculado ahí quedaba vacío para
    siempre en ese proceso, así que las `sub_*` no se ofrecían nunca.
    """

    async def _predicate(_connection_types: set[str]) -> bool:
        from core.agents.spec import enabled_packs, load_active_spec

        try:
            provider_ids = set(subscription_registry.registered_provider_ids())
            if not provider_ids:
                return False
            spec = await load_active_spec()
            return bool(enabled_packs(spec) & provider_ids)
        except Exception:
            return False

    return _predicate


# Smoke de test 2026-10-20 (chat web, «quiero cambiar mi plan»): el visitante no estaba ligado a la
# suscripción y el turno terminó sin salida. Sin verificar la identidad no se busca la suscripción
# de nadie por correo o documento (eso diría quién es suscriptor): se ofrecen las dos salidas.
SIN_SUSCRIPCION_HINT = (
    "Esta conversación no está ligada a una suscripción. Si el cliente dice que ya tiene una (por "
    "ejemplo, la contrató desde otro chat o número), no la busques ni le pidas datos para buscarla: "
    "ofrécele pasar con una persona del equipo que verifique su identidad (escalate_to_human) o, si "
    "prefiere, contratar un plan."
)


def _view_como_resultado(view: SubscriptionView | None) -> dict[str, Any]:
    if view is None:
        return {"encontrado": False, "mensaje": "No tenés una suscripción activa.", "hint": SIN_SUSCRIPCION_HINT}
    return {"encontrado": True, **view}


class SubscriptionSkill(BaseSkill):
    name = "subscriptions"
    description = (
        "Gestión de la suscripción del cliente: consultar, cobrar el período con "
        "un link de pago, pausar, cancelar y cambiar de plan. Despacha al "
        "proveedor de suscripciones del negocio."
    )
    keywords: ClassVar[list[str]] = []  # siempre activa; el predicado filtra por negocio

    def get_tools(self) -> list[MCPTool]:
        """Tools del core: siempre disponibles, fuera del flujo (HU-FWK-01, ADR-0109). Las del
        procedimiento `cambiar_plan` (HU-AOS-08) no: ningún `flow.json` las nombra, así que el loop
        solo las ofrece cuando el motor las pone en la etapa de una tarea en foco."""
        return [
            t if t.name in SOLO_PROCEDIMIENTO else replace(t, always_available=True)
            for t in self._build_tools()
        ]

    def _build_tools(self) -> list[MCPTool]:
        predicate = _proveedor_habilitado()
        return [
            MCPTool(
                name="sub_consultar",
                description=(
                    "Consulta la suscripción del cliente: plan, estado, valor del período y "
                    "próximo cargo. Úsala cuando pregunte por su suscripción o mensualidad."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.consultar,
                connection_predicate=predicate,
            ),
            MCPTool(
                name="sub_link_pago",
                description=(
                    "Genera el link/QR de pago del cargo del período pendiente de la "
                    "suscripción. Úsala cuando el cliente quiera pagar su mensualidad."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.link_pago,
                connection_predicate=predicate,
            ),
            MCPTool(
                name="sub_pausar",
                description=(
                    "Pausa la suscripción del cliente (suspende cargos). Solo cuando lo "
                    "pida y confirme."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.pausar,
                connection_predicate=predicate,
            ),
            MCPTool(
                name="sub_cancelar",
                description=(
                    "Cancela la suscripción del cliente (fin del servicio). Solo cuando "
                    "lo pida explícitamente y confirme."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.cancelar,
                connection_predicate=predicate,
            ),
            MCPTool(
                name="sub_cambiar_plan",
                description=(
                    "Cambia la suscripción del cliente a otro plan del catálogo. Pasá solo el SKU "
                    "del plan nuevo: el precio lo pone el sistema. Subir de plan genera un cobro "
                    "por la diferencia prorrateada (el plan cambia al pagarlo); bajar de plan queda "
                    "programado para el próximo período, sin reembolso."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "plan_code": {
                            "type": "string",
                            "description": "SKU del plan nuevo, tal cual lo devolvió el catálogo (p. ej. PLAN-PREMIUM).",
                        },
                    },
                    "required": ["plan_code"],
                },
                handler=self.cambiar_plan,
                # Con el motor activo el cambio de plan va por el procedimiento (botón con el
                # monto del servidor): esta tool no se ofrece (HU-AOS-08). Sin la bandera, igual.
                connection_predicate=sin_motor_activo(predicate),
            ),
            MCPTool(
                name="sub_cotizar_cambio_plan",
                description=(
                    "Cotiza el cambio de la suscripción del cliente a otro plan del catálogo, sin "
                    "cambiar nada: precio del plan nuevo, tipo de cambio y monto a pagar hoy "
                    "(diferencia prorrateada). Pasá solo el SKU del plan nuevo."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "plan_code": {"type": "string", "description": "SKU del plan nuevo (p. ej. PLAN-PREMIUM)."},
                    },
                    "required": ["plan_code"],
                },
                handler=self.cotizar_cambio_plan,
                connection_predicate=solo_con_motor_activo(predicate),
            ),
            MCPTool(
                name="sub_aplicar_cambio_plan",
                description=(
                    "Aplica el cambio de plan cotizado. Pasá el SKU y el monto_a_pagar EXACTO que "
                    "devolvió sub_cotizar_cambio_plan: si no coincide con el del sistema no se "
                    "aplica. Pide la confirmación del cliente antes de ejecutarse."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "plan_code": {"type": "string", "description": "SKU del plan nuevo."},
                        "monto_a_pagar": {
                            "type": "number",
                            "description": "monto_a_pagar de la cotización (0 si el cambio no cobra hoy).",
                        },
                    },
                    "required": ["plan_code", "monto_a_pagar"],
                },
                handler=self.aplicar_cambio_plan,
                connection_predicate=solo_con_motor_activo(predicate),
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def consultar(self, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        cap = await _capability(ctx)
        suscripciones = await cap.list_for_customer(ctx.customer_id)
        activas = [s for s in suscripciones if s.get("estado") != "cancelada"]
        return _view_como_resultado(activas[0] if activas else None)

    async def link_pago(self, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        cap = await _capability(ctx)
        sub = await _suscripcion_del_cliente(ctx)
        if sub is None:
            return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
        link = await cap.charge_link(uuid.UUID(sub["subscription_id"]))
        resultado: dict[str, Any] = {"encontrado": True, "plan": sub.get("plan"), **link}
        if link.get("url"):
            # El link exacto lo adjunta el core (SafeRenderer), igual que el de un pedido:
            # si el modelo no copia la URL, el cliente igual la recibe (antes se quedaba
            # sin link; visto con los journeys de HU-SUB-12).
            from core.events.core_handlers import format_amount

            monto = format_amount(link.get("monto"), sub.get("moneda"))
            resultado["_safe_append"] = (
                f"💳 Podés pagar {sub.get('plan') or 'tu suscripción'} ({link.get('periodo')}, {monto}) aquí: {link['url']}"
            )
        return resultado

    async def pausar(self, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        cap = await _capability(ctx)
        sub = await _suscripcion_del_cliente(ctx)
        if sub is None:
            return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
        vista = await cap.pause(uuid.UUID(sub["subscription_id"]), actor="asistente")
        return {"encontrado": True, **vista}

    async def cancelar(self, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        cap = await _capability(ctx)
        sub = await _suscripcion_del_cliente(ctx)
        if sub is None:
            return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
        vista = await cap.cancel(uuid.UUID(sub["subscription_id"]), actor="asistente")
        return {"encontrado": True, **vista}

    async def cambiar_plan(self, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Cambio de plan con el precio del CATÁLOGO del servidor (HU-SUB-07, ADR-0111).

        El LLM solo dice a qué plan (`plan_code`); precio, moneda y periodicidad salen del
        catálogo del negocio. El proveedor decide: upgrade → cargo por la diferencia
        prorrateada (el plan cambia al pagarlo), downgrade → programado al próximo período.
        """
        from core.subscriptions import plan_catalog_registry

        cap = await _capability(ctx)
        sub = await _suscripcion_del_cliente(ctx)
        if sub is None:
            return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
        codigo = str(arguments.get("plan_code") or "").strip()
        if not codigo:
            raise SubscriptionError("Falta el código del plan nuevo.", code="plan_invalido")
        tenant_id = get_current_tenant_id()
        catalogo = await plan_catalog_registry.para_negocio(tenant_id) if tenant_id else None
        encontrado = await catalogo.plan_por_codigo(codigo) if catalogo else None
        if encontrado is None:
            raise SubscriptionError(
                f"No encontré el plan {codigo} en el catálogo del negocio.", code="plan_no_encontrado"
            )
        variant_id, plan = encontrado
        vista = await cap.change_plan(uuid.UUID(sub["subscription_id"]), variant_id=variant_id, plan=plan)
        resultado: dict[str, Any] = {"encontrado": True, **vista}
        cargo = vista.get("cargo_cambio") or {}
        if cargo.get("link"):
            from core.events.core_handlers import format_amount

            resultado["_safe_append"] = (
                f"💳 Para pasar a {plan.get('nombre')} pagás la diferencia de "
                f"{format_amount(cargo.get('monto'), plan.get('moneda'))} aquí: {cargo['link']}"
            )
        elif vista.get("cambio") == "pendiente_pago":
            resultado["hint"] = (
                "El pago en línea no está disponible para este monto: ofrecé transferencia. "
                "El plan cambia cuando se confirme el pago."
            )
        return resultado

    # ------------------------------------------------- procedimiento `cambiar_plan` (HU-AOS-08)
    async def cotizar_cambio_plan(self, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await cotizacion_del_cliente(ctx, str(arguments.get("plan_code") or ""))

    async def aplicar_cambio_plan(self, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """El monto confirmado se compara con el del servidor dentro de `change_plan` (misma
        transacción); nunca se usa como monto (adenda de ADR-0111)."""
        monto = arguments.get("monto_a_pagar")
        if isinstance(monto, bool) or not isinstance(monto, int | float | str):
            raise SubscriptionError("Falta el monto_a_pagar de la cotización.", code="monto_distinto")
        cap, sub, variant_id, plan = await _cambio_cotizable(ctx, str(arguments.get("plan_code") or ""))
        vista = await cap.change_plan(  # type: ignore[call-arg]
            uuid.UUID(sub["subscription_id"]), variant_id=variant_id, plan=plan, monto_esperado=monto
        )
        cargo = vista.get("cargo_cambio") or {}
        resultado: dict[str, Any] = {
            "plan": plan.get("nombre"),
            "cambio": vista.get("cambio"),
            "monto_a_pagar": float(cargo.get("monto") or 0),
            "link_pago": cargo.get("link") or None,
            "proximo_cargo": vista.get("proximo_cargo"),
        }
        if cargo.get("link"):
            from core.events.core_handlers import format_amount

            resultado["_safe_append"] = (
                f"💳 Para pasar a {plan.get('nombre')} pagás la diferencia de "
                f"{format_amount(cargo.get('monto'), plan.get('moneda'))} aquí: {cargo['link']}"
            )
        elif vista.get("cambio") == "pendiente_pago":
            resultado["hint"] = (
                "El pago en línea no está disponible para este monto: ofrecé transferencia. "
                "El plan cambia cuando se confirme el pago."
            )
        return resultado


async def _cambio_cotizable(ctx: ToolContext, plan_code: str) -> tuple[Any, SubscriptionView, str, PlanSnapshot]:
    """(proveedor, suscripción vigente, variant_id, plan) o `SubscriptionError`. Todo camino
    sin efecto termina en error: la tarea del procedimiento solo avanza con un resultado real."""
    cap = await _capability(ctx)
    if not soporta_cotizacion(cap):
        raise SubscriptionError("Este proveedor de suscripciones no cotiza cambios de plan.", code="sin_cotizacion")
    sub = await _suscripcion_del_cliente(ctx)
    if sub is None:
        raise SubscriptionError("El cliente no tiene una suscripción activa.", code="sin_suscripcion")
    variant_id, plan = await plan_del_catalogo(plan_code)
    return cap, sub, variant_id, plan


async def cotizacion_del_cliente(ctx: ToolContext, plan_code: str) -> dict[str, Any]:
    """Cotización del cambio de la suscripción del cliente de la conversación al plan `plan_code`
    (sin efectos). La usan `sub_cotizar_cambio_plan` y el resolver `plan_destino` del
    procedimiento, así los dos dicen lo mismo. Lanza `SubscriptionError`."""
    cap, sub, variant_id, plan = await _cambio_cotizable(ctx, plan_code)
    cotizacion = await cotizar_cambio(cap, uuid.UUID(sub["subscription_id"]), variant_id=variant_id, plan=plan)
    return {"plan_code": str(plan.get("plan_code") or plan_code.strip()), **cotizacion}


# Tools que solo ofrece el motor de procedimientos (no son `always_available`).
SOLO_PROCEDIMIENTO = frozenset({"sub_cotizar_cambio_plan", "sub_aplicar_cambio_plan"})


async def plan_del_catalogo(codigo: str) -> tuple[str, PlanSnapshot]:
    """(variant_id, snapshot) del plan por SKU en el catálogo del negocio (ADR-0111), o error."""
    from core.subscriptions import plan_catalog_registry

    codigo = codigo.strip()[:100]
    if not codigo:
        raise SubscriptionError("Falta el código del plan nuevo.", code="plan_invalido")
    tenant_id = get_current_tenant_id()
    catalogo = await plan_catalog_registry.para_negocio(tenant_id) if tenant_id else None
    encontrado = await catalogo.plan_por_codigo(codigo) if catalogo else None
    if encontrado is None:
        # Sin repetir el código: lo escribió el modelo (y puede venir del mensaje del cliente).
        raise SubscriptionError("Ese plan no está en el catálogo del negocio.", code="plan_no_encontrado")
    return encontrado

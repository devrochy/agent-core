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

from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.subscriptions import (
    SubscriptionCapability,
    SubscriptionError,
    SubscriptionView,
    capability_del_negocio,
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


def _proveedor_habilitado(provider_ids: frozenset[str]) -> Callable[[set[str]], Awaitable[bool]]:
    """Predicado de oferta: solo tenants con un proveedor de suscripciones activo."""

    async def _predicate(_connection_types: set[str]) -> bool:
        from core.agents.spec import enabled_packs, load_active_spec

        try:
            spec = await load_active_spec()
            return bool(enabled_packs(spec) & provider_ids)
        except Exception:
            return False

    return _predicate


def _view_como_resultado(view: SubscriptionView | None) -> dict[str, Any]:
    if view is None:
        return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
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
        """Tools del core: siempre disponibles, fuera del flujo (HU-FWK-01, ADR-0109)."""
        return [replace(t, always_available=True) for t in self._build_tools()]

    def _build_tools(self) -> list[MCPTool]:
        providers = frozenset(subscription_registry.registered_provider_ids())
        predicate = _proveedor_habilitado(providers)
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
                connection_predicate=predicate,
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

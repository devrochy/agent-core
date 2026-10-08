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
    PlanSnapshot,
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
                    "Cambia la suscripción a otro plan del catálogo. Confirmá primero los "
                    "datos reales del plan con las tools de commerce (informar_plan/"
                    "consultar_inventario) y pasá precio, moneda y periodicidad tal cual "
                    "te los dio el catálogo, nunca inventados."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "plan_code": {"type": "string", "description": "Código/SKU del plan nuevo."},
                        "nombre": {"type": "string", "description": "Nombre del plan nuevo."},
                        "precio": {"type": "number", "description": "Precio por período."},
                        "moneda": {"type": "string", "description": "Moneda (COP, USD…)."},
                        "periodicidad": {
                            "type": "string",
                            "description": "mensual | trimestral | anual",
                        },
                    },
                    "required": ["plan_code", "nombre", "precio", "moneda", "periodicidad"],
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
        return {"encontrado": True, "plan": sub.get("plan"), **link}

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
        cap = await _capability(ctx)
        sub = await _suscripcion_del_cliente(ctx)
        if sub is None:
            return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
        plan: PlanSnapshot = {
            "plan_code": str(arguments.get("plan_code") or "").strip(),
            "nombre": str(arguments.get("nombre") or "").strip(),
            "precio": float(arguments.get("precio") or 0),
            "moneda": str(arguments.get("moneda") or "COP").strip().upper(),
            "periodicidad": str(arguments.get("periodicidad") or "mensual").strip().lower(),
        }
        if not plan["plan_code"] or not plan["nombre"]:
            raise SubscriptionError(
                "Faltan datos del plan nuevo (plan_code y nombre).", code="plan_invalido"
            )
        vista = await cap.change_plan(uuid.UUID(sub["subscription_id"]), variant_id=plan["plan_code"], plan=plan)
        return {"encontrado": True, **vista}

"""Skills de pack-subscriptions (ADR-0083, planes como productos).

El plan es un producto de commerce (variante con `billing_period`); la
suscripción se habilita cuando el pedido se paga (hook de commerce → registry).
Este pack NO informa planes (eso lo hace commerce) y NO crea suscripciones por
tool: solo gestiona la suscripción existente y cobra el período.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_subscriptions.config import SubscriptionConfig
from pack_subscriptions.models import Subscription, SubscriptionCharge

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"


def _money(v: Decimal | float | None, moneda: str = "COP") -> str:
    if v is None:
        return "—"
    return f"{moneda} {Decimal(v):,.0f}".replace(",", ".")


def _proximo_cargo(desde: datetime) -> datetime:
    return (desde + timedelta(days=30)).replace(second=0, microsecond=0)


class SubscriptionsSkill(BaseSkill):
    name = "subscriptions"
    description = (
        "Gestión de la suscripción del cliente: consultar, pausar, cancelar, "
        "renovar y cobrar el período con un link de pago."
    )
    keywords: list[str] = []  # noqa: RUF012 -- siempre activa con el pack habilitado

    async def _config(self) -> SubscriptionConfig:
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
        if config is None:
            config = SubscriptionConfig()
        return config

    async def _suscripcion_activa(self, session, customer_id: uuid.UUID) -> Subscription | None:
        return (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == get_current_tenant_id(),
                    Subscription.customer_id == customer_id,
                    Subscription.estado.in_(("trialing", "activa", "pausada")),
                    Subscription.deleted_at.is_(None),
                )
                .order_by(Subscription.created_at.desc())
            )
        ).scalars().first()

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="sub_consultar_suscripcion",
                description=(
                    "Consulta la suscripción del cliente: plan, estado, valor del período y "
                    "próximo cargo. Úsala cuando pregunte por su suscripción o mensualidad."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.consultar_suscripcion,
            ),
            MCPTool(
                name="sub_pausar_suscripcion",
                description="Pausa la suscripción del cliente (suspende cargos). Solo cuando lo pida y confirme.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.pausar,
            ),
            MCPTool(
                name="sub_cancelar_suscripcion",
                description="Cancela la suscripción del cliente (fin del servicio). Solo cuando lo pida explícitamente.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.cancelar,
            ),
            MCPTool(
                name="sub_renovar_suscripcion",
                description="Reactiva una suscripción pausada y reprograma el próximo cargo.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.renovar,
            ),
            MCPTool(
                name="sub_link_pago",
                description=(
                    "Genera el link/QR de pago del cargo del período pendiente de la suscripción. "
                    "Úsala cuando el cliente quiera pagar su mensualidad."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.link_pago,
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def consultar_suscripcion(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._suscripcion_activa(session, customer_id) if customer_id else None
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
            cargos = (
                await session.execute(
                    select(SubscriptionCharge)
                    .where(
                        SubscriptionCharge.tenant_id == get_current_tenant_id(),
                        SubscriptionCharge.subscription_id == sub.id,
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                    .order_by(SubscriptionCharge.created_at.desc())
                    .limit(6)
                )
            ).scalars().all()
            return {
                "encontrado": True,
                "suscripcion_id": str(sub.id),
                "plan": sub.concepto,
                "estado": sub.estado,
                "valorPeriodo": float(sub.valor_periodo or 0),
                "moneda": sub.moneda,
                "proximoCargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
                "cargos": [
                    {"periodo": c.periodo, "monto": float(c.monto or 0), "estado": c.estado}
                    for c in cargos
                ],
            }

    async def pausar(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return await self._cambiar_estado(context, "pausada")

    async def cancelar(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._suscripcion_activa(session, customer_id) if customer_id else None
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
            sub.estado = "cancelada"
            sub.fecha_cancelacion = datetime.now(UTC)
            sub.proximo_cargo = None
            await session.flush()
            return {"estado": "cancelada", "suscripcion_id": str(sub.id)}

    async def _cambiar_estado(self, context: ToolContext, estado: str) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._suscripcion_activa(session, customer_id) if customer_id else None
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
            sub.estado = estado
            if estado == "pausada":
                sub.proximo_cargo = None
            await session.flush()
            return {"estado": estado, "suscripcion_id": str(sub.id)}

    async def renovar(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._suscripcion_activa(session, customer_id) if customer_id else None
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción."}
            sub.estado = "activa"
            sub.fecha_cancelacion = None
            if sub.proximo_cargo is None:
                sub.proximo_cargo = _proximo_cargo(datetime.now(UTC))
            await session.flush()
            return {"estado": "activa", "suscripcion_id": str(sub.id), "proximoCargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None}

    async def link_pago(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._suscripcion_activa(session, customer_id) if customer_id else None
            if sub is None or sub.estado == "cancelada":
                return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
            periodo = datetime.now(UTC).strftime("%Y-%m")
            cargo = (
                await session.execute(
                    select(SubscriptionCharge)
                    .where(
                        SubscriptionCharge.tenant_id == get_current_tenant_id(),
                        SubscriptionCharge.subscription_id == sub.id,
                        SubscriptionCharge.periodo == periodo,
                        SubscriptionCharge.estado == "pendiente",
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                )
            ).scalars().first()
            if cargo is None:
                cargo = SubscriptionCharge(
                    tenant_id=get_current_tenant_id(),
                    subscription_id=sub.id,
                    periodo=periodo,
                    monto=sub.valor_periodo or 0,
                    estado="pendiente",
                )
                session.add(cargo)
                await session.flush()

        from core.agents.spec import enabled_packs, load_active_spec
        from core.payments import PaymentCapabilityError, payment_registry

        spec = await load_active_spec()
        cap = payment_registry.capability_for(enabled_packs(spec))
        if cap is None:
            return {
                "encontrado": True,
                "sin_pasarela": True,
                "monto": float(cargo.monto or 0),
                "mensaje": "El pago en línea no está configurado; coordinemos el pago por otro medio.",
            }
        reference = f"sub-{cargo.id}"
        try:
            link = await cap.create_link(
                reference=reference, amount=float(cargo.monto or 0), description=f"Suscripción {sub.concepto}"
            )
        except PaymentCapabilityError:
            # Rechazo de NEGOCIO (p. ej. monto bajo el mínimo de la pasarela):
            # resultado controlado, el pack SIGUE habilitado (HU-ARCH-18). El
            # asistente ofrece una alternativa.
            return {
                "encontrado": True,
                "status": "monto_bajo_minimo",
                "monto": float(cargo.monto or 0),
                "mensaje": "El pago en línea no está disponible para este monto; coordinemos el pago por otro medio.",
            }
        async with get_session() as session:
            # `cargo` quedó detachado de la sesión anterior: re-buscar por id para
            # persistir la referencia del link (escribir sobre el objeto detached
            # no se guarda).
            fila = await session.get(SubscriptionCharge, cargo.id)
            if fila is not None:
                fila.referencia = link.reference
                await session.flush()
        return {
            "encontrado": True,
            "plan": sub.concepto,
            "monto": float(cargo.monto or 0),
            "referencia": link.reference,
            "url": link.url,
            "periodo": periodo,
        }
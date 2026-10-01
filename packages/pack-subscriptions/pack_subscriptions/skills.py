"""Skills de pack-subscriptions (ADR-0083): planes y suscripciones del cliente.

El asistente informa planes, consulta/crea/cambia/pausa/cancela/renueva la
suscripción y cobra el período. En la Opción B la **compra** del plan ocurre por
commerce (catálogo → pedido → link); al pagarse, `activate_for_customer` crea la
suscripción automáticamente. Nunca afirmar un cambio sin la tool del turno.
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
from pack_subscriptions.models import Plan, Subscription, SubscriptionCharge

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"


def _money(v: Decimal | float | None, moneda: str = "COP") -> str:
    if v is None:
        return "—"
    return f"{moneda} {Decimal(v):,.0f}".replace(",", ".")


def _proximo_cargo(desde: datetime, periodicidad: str) -> datetime:
    """Fecha del siguiente cargo (mensual por ahora), estricta (posterior a `desde`)."""
    if periodicidad != "mensual":
        periodicidad = "mensual"
    nxt = desde + timedelta(days=30)
    return nxt.replace(hour=desde.hour, minute=desde.minute, second=0, microsecond=0)


class SubscriptionsSkill(BaseSkill):
    name = "subscriptions"
    description = (
        "Planes y suscripciones del cliente: informar planes, consultar/crear/cambiar/"
        "pausar/cancelar/renovar la suscripción y cobrar el período."
    )
    keywords: list[str] = []  # noqa: RUF012 -- siempre activa con el pack habilitado

    async def _config(self) -> SubscriptionConfig:
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
        if config is None:
            config = SubscriptionConfig()
        return config

    async def _plan(self, session, plan_id: str | None) -> Plan | None:
        if not plan_id:
            return None
        try:
            pid = uuid.UUID(str(plan_id))
        except (ValueError, TypeError):
            return None
        return (
            await session.execute(
                select(Plan).where(
                    Plan.tenant_id == get_current_tenant_id(),
                    Plan.id == pid,
                    Plan.activo.is_(True),
                    Plan.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

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
                name="sub_informar_planes",
                description=(
                    "Lista los planes/servicios recurrentes disponibles (nombre, precio, "
                    "periodicidad, prueba). Úsala SIEMPRE antes de hablar de precios de planes: "
                    "no inventes montos."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.informar_planes,
            ),
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
                name="sub_crear_suscripcion",
                description=(
                    "Crea la suscripción del cliente a un plan. Usar SOLO cuando el cliente "
                    "confirmó el plan explícitamente (o cuando un plan se compró por la tienda "
                    "y hay que activarlo). El valor sale del plan, nunca lo inventes."
                ),
                parameters={
                    "type": "object",
                    "properties": {"plan_id": {"type": "string", "description": "ID del plan (de sub_informar_planes)"}},
                    "required": ["plan_id"],
                },
                handler=self.crear_suscripcion,
            ),
            MCPTool(
                name="sub_cambiar_plan",
                description=(
                    "Cambia el plan de la suscripción del cliente. Usar solo cuando lo pida "
                    "y confirme el nuevo plan."
                ),
                parameters={
                    "type": "object",
                    "properties": {"plan_id": {"type": "string", "description": "ID del nuevo plan"}},
                    "required": ["plan_id"],
                },
                handler=self.cambiar_plan,
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
    async def informar_planes(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Plan)
                    .where(
                        Plan.tenant_id == get_current_tenant_id(),
                        Plan.activo.is_(True),
                        Plan.deleted_at.is_(None),
                    )
                    .order_by(Plan.precio.asc())
                )
            ).scalars().all()
        return {
            "planes": [
                {
                    "plan_id": str(p.id),
                    "nombre": p.nombre,
                    "precio": float(p.precio or 0),
                    "precio_label": _money(p.precio, p.moneda),
                    "periodicidad": p.periodicidad,
                    "trial_dias": p.trial_dias,
                    "descripcion": p.descripcion,
                }
                for p in rows
            ],
            "total": len(rows),
        }

    async def _crear_suscripcion_row(
        self, session, *, customer_id: uuid.UUID, plan: Plan, reference: str = ""
    ) -> Subscription:
        cfg = await self._config()
        now = datetime.now(UTC)
        trial = int(plan.trial_dias or cfg.trial_dias) > 0
        trial_termina = now + timedelta(days=int(plan.trial_dias or cfg.trial_dias)) if trial else None
        sub = Subscription(
            tenant_id=get_current_tenant_id(),
            customer_id=customer_id,
            plan_id=plan.id,
            variante_id=plan.variante_id,
            concepto=plan.nombre,
            estado="trialing" if trial else "activa",
            valor_periodo=plan.precio or 0,
            moneda=plan.moneda or cfg.moneda,
            trial_termina=trial_termina,
            proximo_cargo=_proximo_cargo(now, plan.periodicidad or cfg.periodicidad),
            fecha_alta=now,
        )
        session.add(sub)
        await session.flush()
        return sub

    async def crear_suscripcion(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        if customer_id is None:
            return {"error": "cliente_no_identificado"}
        async with get_session() as session:
            plan = await self._plan(session, arguments.get("plan_id"))
            if plan is None:
                return {"error": "plan_no_encontrado", "mensaje": "No encontré ese plan. Usá sub_informar_planes."}
            existente = await self._suscripcion_activa(session, customer_id)
            if existente is not None:
                return {
                    "ya_existe": True,
                    "suscripcion_id": str(existente.id),
                    "plan": existente.concepto,
                    "estado": existente.estado,
                }
            sub = await self._crear_suscripcion_row(session, customer_id=customer_id, plan=plan)
            return {
                "creada": True,
                "suscripcion_id": str(sub.id),
                "plan": sub.concepto,
                "estado": sub.estado,
                "valorPeriodo": float(sub.valor_periodo or 0),
            }

    async def _sub(self, session, customer_id: uuid.UUID) -> Subscription | None:
        return await self._suscripcion_activa(session, customer_id)

    async def consultar_suscripcion(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._sub(session, customer_id) if customer_id else None
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

    async def cambiar_plan(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._sub(session, customer_id) if customer_id else None
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción activa."}
            plan = await self._plan(session, arguments.get("plan_id"))
            if plan is None:
                return {"error": "plan_no_encontrado"}
            sub.plan_id = plan.id
            sub.variante_id = plan.variante_id
            sub.concepto = plan.nombre
            sub.valor_periodo = plan.precio or 0
            sub.moneda = plan.moneda or "COP"
            await session.flush()
            return {"cambiada": True, "suscripcion_id": str(sub.id), "plan": sub.concepto}

    async def pausar(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return await self._cambiar_estado(context, "pausada")

    async def cancelar(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._sub(session, customer_id) if customer_id else None
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
            sub = await self._sub(session, customer_id) if customer_id else None
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
            sub = await self._sub(session, customer_id) if customer_id else None
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción."}
            sub.estado = "activa"
            sub.fecha_cancelacion = None
            if sub.proximo_cargo is None:
                sub.proximo_cargo = _proximo_cargo(datetime.now(UTC), "mensual")
            await session.flush()
            return {"estado": "activa", "suscripcion_id": str(sub.id), "proximoCargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None}

    async def link_pago(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        customer_id = getattr(context, "customer_id", None)
        async with get_session() as session:
            sub = await self._sub(session, customer_id) if customer_id else None
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
        from core.payments import payment_registry

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
        link = await cap.create_link(
            reference=reference, amount=float(cargo.monto or 0), description=f"Suscripción {sub.concepto}"
        )
        async with get_session() as session:
            cargo.referencia = link.reference
        return {
            "encontrado": True,
            "plan": sub.concepto,
            "monto": float(cargo.monto or 0),
            "referencia": link.reference,
            "url": link.url,
            "periodo": periodo,
        }
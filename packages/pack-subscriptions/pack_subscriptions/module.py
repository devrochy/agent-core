"""SubscriptionsModule: planes y suscripciones de cliente final (ADR-0083).

Registra su `SubscriptionCapability` en `core.subscriptions.subscription_registry`
para que **commerce** la consuma sin importarla (invariante nº5): al pagarse un
pedido que incluye un Plan (variante vinculada), se activa la suscripción del
`Customer`. También escucha `payment.confirmed` para marcar los cargos pagados.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseModule, GuardrailResult
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope
from core.subscriptions import subscription_registry
from pack_subscriptions.config import SubscriptionConfig
from pack_subscriptions.models import Plan, Subscription, SubscriptionCharge
from pack_subscriptions.skills import SubscriptionsSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"

_CREAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:activ\w*|creada|registrada)|(?:activ\w*|cre\w*)\s+[^.]{0,30}suscripci", re.IGNORECASE)
_CAMBIAR = re.compile(r"(?:camb\w+|pas\w+)\s+(?:tu\s+)?(?:plan|suscripci)|plan\s+(?:cambiado|actualizado)", re.IGNORECASE)
_PAUSAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?pausad\w*|paus\w+\s+(?:tu\s+)?suscripci", re.IGNORECASE)
_CANCELAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:cancel\w*|anulad\w*)|cancel\w+\s+(?:tu\s+)?suscripci", re.IGNORECASE)
_RENOVAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:renov\w*|reactivad\w*)|(?:renov\w*|reactiv\w+)\s+(?:tu\s+)?suscripci", re.IGNORECASE)


async def crear_suscripcion_para(
    *, customer_id: uuid.UUID, plan: Plan, reference: str = ""
) -> Subscription:
    """Crea/activa la suscripción de un `Customer` a un plan (idempotente por cliente)."""
    async with get_session() as session:
        existente = (
            await session.execute(
                select(Subscription).where(
                    Subscription.tenant_id == get_current_tenant_id(),
                    Subscription.customer_id == customer_id,
                    Subscription.estado.in_(("trialing", "activa", "pausada")),
                    Subscription.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existente is not None:
            return existente
        now = datetime.now(UTC)
        trial = int(plan.trial_dias or 0) > 0
        sub = Subscription(
            tenant_id=get_current_tenant_id(),
            customer_id=customer_id,
            plan_id=plan.id,
            variante_id=plan.variante_id,
            concepto=plan.nombre,
            estado="trialing" if trial else "activa",
            valor_periodo=plan.precio or 0,
            moneda=plan.moneda or "COP",
            trial_termina=(now + timedelta(days=int(plan.trial_dias or 0))) if trial else None,
            proximo_cargo=_proximo(now),
            fecha_alta=now,
        )
        session.add(sub)
        await session.flush()
        return sub


def _proximo(desde: datetime) -> datetime:
    return (desde + timedelta(days=30)).replace(second=0, microsecond=0)


class _SubscriptionCapability:
    """Capacidad registrada en el registry del core (consumida por commerce)."""

    plugin_id = PLUGIN_ID

    async def plan_for_variant(self, variant_id: str) -> dict[str, Any] | None:
        try:
            vid = uuid.UUID(str(variant_id))
        except (ValueError, TypeError):
            return None
        async with tenant_scope(get_current_tenant_id()):
            async with get_session() as session:
                plan = (
                    await session.execute(
                        select(Plan).where(
                            Plan.tenant_id == get_current_tenant_id(),
                            Plan.variante_id == vid,
                            Plan.activo.is_(True),
                            Plan.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
        if plan is None:
            return None
        return {
            "plan_id": str(plan.id),
            "nombre": plan.nombre,
            "precio": float(plan.precio or 0),
            "periodicidad": plan.periodicidad,
            "trial_dias": plan.trial_dias,
        }

    async def activate_for_customer(
        self, *, customer_id: uuid.UUID, variant_id: str, reference: str = ""
    ) -> dict[str, Any]:
        info = await self.plan_for_variant(variant_id)
        if info is None:
            return {"activada": False, "mensaje": "La variante no corresponde a un plan."}
        async with tenant_scope(get_current_tenant_id()):
            async with get_session() as session:
                plan = await session.get(Plan, uuid.UUID(info["plan_id"]))
        sub = await crear_suscripcion_para(customer_id=customer_id, plan=plan, reference=reference)
        return {"activada": True, "suscripcion_id": str(sub.id), "plan": sub.concepto, "estado": sub.estado}


class SubscriptionsModule(BaseModule):
    id = PLUGIN_ID
    name = "Suscripciones"
    version = "0.1.0"
    description = "Planes y suscripciones de cliente final (ADR-0083)"
    requires_config = True
    config_schema = SubscriptionConfig
    declared_skills: list[type] = [SubscriptionsSkill]  # noqa: RUF012
    admin_menu: list[dict[str, str]] = [  # noqa: RUF012
        {"label": "Planes", "path": "/subscriptions/planes", "section": "Configuración", "section_key": "pack_subscriptions:planes"},
        {"label": "Suscripciones", "path": "/subscriptions/lista", "section": "Operación", "section_key": "pack_subscriptions:suscripciones"},
    ]

    def on_activate(self, host: MCPHost) -> None:
        host.register_skill(SubscriptionsSkill())
        host.register_guardrail("subs_completion_check", self._completion_guardrail, priority=6)
        subscription_registry.register(PLUGIN_ID, _SubscriptionCapability())
        host.subscribe("payment.confirmed", self._on_payment_confirmed)

    def on_deactivate(self) -> None:
        subscription_registry.unregister(PLUGIN_ID)

    async def seed_data(self, tenant_id: uuid.UUID, data: dict[str, Any]) -> list[str]:
        """Seed de planes (ADR-0083, #154): crea `Plan` idempotentes desde
        `TENANTS_ROOT/<slug>/pack_subscriptions.json`. `variante_sku` ya fue
        resuelto a `variante_id` por el CLI (este pack NO lee tablas de commerce).
        """
        planes = data.get("planes", []) if isinstance(data, dict) else []
        if not planes:
            return []
        steps: list[str] = []
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                for p in planes:
                    nombre = (p.get("nombre") or "").strip()
                    if not nombre:
                        continue
                    variante_id = None
                    if p.get("variante_id"):
                        try:
                            variante_id = uuid.UUID(str(p["variante_id"]))
                        except (ValueError, TypeError):
                            variante_id = None
                    existente = (
                        await session.execute(
                            select(Plan).where(
                                Plan.tenant_id == tenant_id,
                                Plan.nombre == nombre,
                                Plan.deleted_at.is_(None),
                            )
                        )
                    ).scalars().first()
                    if existente is not None:
                        if variante_id and existente.variante_id != variante_id:
                            existente.variante_id = variante_id
                        continue
                    session.add(
                        Plan(
                            tenant_id=tenant_id,
                            nombre=nombre,
                            descripcion=p.get("descripcion") or "",
                            precio=p.get("precio") or 0,
                            moneda=p.get("moneda") or "COP",
                            periodicidad=p.get("periodicidad") or "mensual",
                            trial_dias=int(p.get("trial_dias") or 30),
                            limites=p.get("limites"),
                            variante_id=variante_id,
                            activo=bool(p.get("activo", True)),
                        )
                    )
                    steps.append(f"Plan '{nombre}' creado")
                await session.flush()
        return steps

    async def _on_payment_confirmed(self, reference: str, provider: str, provider_reference: str, amount: float) -> None:
        """Marca pagado el cargo del período cuyo `referencia` es `sub-<charge.id>`."""
        if not reference or not reference.startswith("sub-"):
            return
        try:
            charge_id = uuid.UUID(reference[4:])
        except (ValueError, TypeError):
            return
        tenant_id = get_current_tenant_id()
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                cargo = (
                    await session.execute(
                        select(SubscriptionCharge).where(
                            SubscriptionCharge.tenant_id == tenant_id,
                            SubscriptionCharge.id == charge_id,
                        )
                    )
                ).scalar_one_or_none()
                if cargo is None:
                    return
                cargo.estado = "pagado"
                sub = await session.get(Subscription, cargo.subscription_id)
                if sub is not None and sub.estado == "trialing" and sub.trial_termina and sub.trial_termina <= datetime.now(UTC):
                    sub.estado = "activa"
        log.info("subs_charge_pagado", reference=reference)

    async def _completion_guardrail(
        self, response_text: str, tool_results: list[dict[str, Any]]
    ) -> GuardrailResult:
        """Bloquea afirmar cambios de suscripción sin la tool correspondiente (ADR-0083)."""
        called = {r.get("_tool_name") for r in (tool_results or [])}
        checks = [
            (_CREAR, {"sub_crear_suscripcion"}),
            (_CAMBIAR, {"sub_cambiar_plan"}),
            (_PAUSAR, {"sub_pausar_suscripcion"}),
            (_CANCELAR, {"sub_cancelar_suscripcion"}),
            (_RENOVAR, {"sub_renovar_suscripcion"}),
        ]
        for pattern, tools in checks:
            if pattern.search(response_text or "") and not (called & tools):
                return GuardrailResult(
                    passed=False,
                    reason="Afirmación de suscripción sin la tool correspondiente en el turno.",
                    retry_instruction=(
                        f"Afirmaste algo sobre la suscripción pero no llamaste la herramienta "
                        f"({', '.join(sorted(tools))}). Llamala ahora, o si no se pudo, decilo "
                        "explícitamente en vez de prometerlo."
                    ),
                )
        return GuardrailResult(passed=True)
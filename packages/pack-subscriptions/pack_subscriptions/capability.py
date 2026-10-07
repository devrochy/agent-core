"""Implementación del contrato `SubscriptionCapability` v1 (HU-SUB-01, ADR-0106).

Proveedor de referencia sobre `pack_subscriptions_subscriptions`/`_charges`
(ADR-0083: planes como productos de commerce). La lógica de ciclo de vida y de
cobro vivía en `skills.py` (tools propias del pack); desde HU-SUB-01 las tools
`sub_*` están en el core y esta clase es el proveedor que las atiende: el pack
ya NO expone skills al LLM.

Regla del contrato: `charge_link` nunca crea un período futuro.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.subscriptions import (
    ChargeLink,
    PlanSnapshot,
    SubscriptionError,
    SubscriptionView,
)
from pack_subscriptions.models import Subscription, SubscriptionCharge

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"
_ESTADOS_VIGENTES = ("trialing", "activa", "pausada")


def _proximo(desde: datetime) -> datetime:
    return (desde + timedelta(days=30)).replace(second=0, microsecond=0)


def _periodo_actual(ahora: datetime) -> str:
    return ahora.strftime("%Y-%m")


def _view(sub: Subscription, cargos: list[SubscriptionCharge] | None = None) -> SubscriptionView:
    return {
        "subscription_id": str(sub.id),
        "plan_code": str(sub.variante_id) if sub.variante_id else "",
        "plan": sub.concepto,
        "estado": sub.estado,
        "valor_periodo": float(sub.valor_periodo or 0),
        "moneda": sub.moneda,
        "proximo_cargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
        "trial_termina": sub.trial_termina.isoformat() if sub.trial_termina else None,
        "cargos": [
            {"periodo": c.periodo, "monto": float(c.monto or 0), "estado": c.estado}
            for c in (cargos or [])
        ],
    }


async def crear_suscripcion_para(
    *, customer_id: uuid.UUID, variante_id: uuid.UUID | None, plan: dict[str, Any], reference: str = ""
) -> Subscription:
    """Crea/activa la suscripción de un `Customer` a un plan (idempotente por cliente).

    `plan` es el snapshot aportado por commerce/admin: nombre, precio, moneda,
    periodicidad, trial_dias. Este pack NO guarda ni lee el catálogo.
    """
    async with get_session() as session:
        existente = (
            await session.execute(
                select(Subscription).where(
                    Subscription.tenant_id == get_current_tenant_id(),
                    Subscription.customer_id == customer_id,
                    Subscription.estado.in_(_ESTADOS_VIGENTES),
                    Subscription.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existente is not None:
            return existente
        now = datetime.now(UTC)
        trial_dias = int(plan.get("trial_dias") or 0)
        sub = Subscription(
            tenant_id=get_current_tenant_id(),
            customer_id=customer_id,
            variante_id=variante_id,
            concepto=plan.get("nombre") or "Plan",
            estado="trialing" if trial_dias else "activa",
            valor_periodo=plan.get("precio") or 0,
            moneda=plan.get("moneda") or "COP",
            trial_termina=(now + timedelta(days=trial_dias)) if trial_dias else None,
            proximo_cargo=_proximo(now),
            fecha_alta=now,
        )
        session.add(sub)
        await session.flush()
        return sub


class SubscriptionCapabilityImpl:
    """Proveedor de suscripciones del contrato v1 (las 8 operaciones)."""

    provider_id = PLUGIN_ID

    async def _sub_vigente(self, session, subscription_id: uuid.UUID) -> Subscription:
        sub = await session.get(Subscription, subscription_id)
        if sub is None or sub.deleted_at is not None or sub.estado == "cancelada":
            raise SubscriptionError(
                "La suscripción no existe o está cancelada.", code="suscripcion_no_encontrada"
            )
        return sub

    async def _cargos_recientes(
        self, session, subscription_id: uuid.UUID
    ) -> list[SubscriptionCharge]:
        return (
            await session.execute(
                select(SubscriptionCharge)
                .where(
                    SubscriptionCharge.tenant_id == get_current_tenant_id(),
                    SubscriptionCharge.subscription_id == subscription_id,
                    SubscriptionCharge.deleted_at.is_(None),
                )
                .order_by(SubscriptionCharge.created_at.desc())
                .limit(6)
            )
        ).scalars().all()

    async def activate(
        self,
        *,
        customer_id: uuid.UUID,
        variant_id: str,
        plan: PlanSnapshot,
        reference: str = "",
    ) -> SubscriptionView:
        variante_id = None
        try:
            variante_id = uuid.UUID(str(variant_id))
        except (ValueError, TypeError):
            pass
        sub = await crear_suscripcion_para(
            customer_id=customer_id, variante_id=variante_id, plan=plan, reference=reference
        )
        return _view(sub)

    async def get(self, subscription_id: uuid.UUID) -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            cargos = await self._cargos_recientes(session, subscription_id)
            return _view(sub, cargos)

    async def list_for_customer(self, customer_id: uuid.UUID) -> list[SubscriptionView]:
        async with get_session() as session:
            subs = (
                await session.execute(
                    select(Subscription)
                    .where(
                        Subscription.tenant_id == get_current_tenant_id(),
                        Subscription.customer_id == customer_id,
                        Subscription.deleted_at.is_(None),
                    )
                    .order_by(Subscription.created_at.desc())
                )
            ).scalars().all()
            return [_view(s) for s in subs]

    async def pause(
        self, subscription_id: uuid.UUID, *, reason: str = "", actor: str = ""
    ) -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.estado = "pausada"
            sub.proximo_cargo = None
            await session.flush()
            return _view(sub)

    async def resume(self, subscription_id: uuid.UUID, *, actor: str = "") -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.estado = "activa"
            sub.fecha_cancelacion = None
            if sub.proximo_cargo is None:
                sub.proximo_cargo = _proximo(datetime.now(UTC))
            await session.flush()
            return _view(sub)

    async def cancel(
        self, subscription_id: uuid.UUID, *, reason: str = "", actor: str = ""
    ) -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.estado = "cancelada"
            sub.fecha_cancelacion = datetime.now(UTC)
            sub.proximo_cargo = None
            await session.flush()
            return _view(sub)

    async def change_plan(
        self,
        subscription_id: uuid.UUID,
        *,
        variant_id: str,
        plan: PlanSnapshot,
    ) -> SubscriptionView:
        variante_id = None
        try:
            variante_id = uuid.UUID(str(variant_id))
        except (ValueError, TypeError):
            pass
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.variante_id = variante_id
            sub.concepto = plan.get("nombre") or sub.concepto
            if plan.get("precio") is not None:
                sub.valor_periodo = plan["precio"]
            sub.moneda = plan.get("moneda") or sub.moneda
            await session.flush()
            return _view(sub)

    async def charge_link(
        self, subscription_id: uuid.UUID, *, period: str = ""
    ) -> ChargeLink:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            ahora = datetime.now(UTC)
            periodo = period or _periodo_actual(ahora)
            if periodo > _periodo_actual(ahora):
                raise SubscriptionError(
                    "No se cobra un período futuro.", code="periodo_futuro"
                )
            cargo = (
                await session.execute(
                    select(SubscriptionCharge)
                    .where(
                        SubscriptionCharge.tenant_id == get_current_tenant_id(),
                        SubscriptionCharge.subscription_id == subscription_id,
                        SubscriptionCharge.periodo == periodo,
                        SubscriptionCharge.estado == "pendiente",
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                )
            ).scalars().first()
            if cargo is None:
                cargo = SubscriptionCharge(
                    tenant_id=get_current_tenant_id(),
                    subscription_id=subscription_id,
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
            raise SubscriptionError(
                "El pago en línea no está configurado para este negocio; coordinemos "
                "el pago por otro medio.",
                code="sin_pasarela",
            )
        reference = f"sub-{cargo.id}"
        try:
            link = await cap.create_link(
                reference=reference,
                amount=float(cargo.monto or 0),
                description=f"Suscripción {sub.concepto}",
            )
        except PaymentCapabilityError as exc:
            raise SubscriptionError(
                "El pago en línea no está disponible para este monto; coordinemos el "
                "pago por otro medio.",
                code="monto_bajo_minimo",
            ) from exc
        async with get_session() as session:
            fila = await session.get(SubscriptionCharge, cargo.id)
            if fila is not None:
                fila.referencia = link.reference
                await session.flush()
        return {
            "url": link.url,
            "referencia": link.reference,
            "periodo": periodo,
            "monto": float(cargo.monto or 0),
        }

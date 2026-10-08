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
from decimal import Decimal
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
from pack_subscriptions.periodos import PERIODICIDADES, etiqueta_periodo, sumar_periodo

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"
_ESTADOS_VIGENTES = ("trialing", "activa", "pausada")


async def _config():  # type: ignore[no-untyped-def]
    """Config del pack para el negocio actual; defaults si no hay o no carga."""
    from core.plugin_manager import plugin_manager
    from pack_subscriptions.config import SubscriptionConfig

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    return config if isinstance(config, SubscriptionConfig) else SubscriptionConfig()


def _periodicidad(valor: object, por_defecto: str) -> str:
    texto = str(valor or "").strip().lower()
    return texto if texto in PERIODICIDADES else por_defecto


def _en_prueba(sub: Subscription, ahora: datetime) -> bool:
    return sub.estado == "trialing" and sub.trial_termina is not None and sub.trial_termina > ahora


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

    HU-SUB-06 (D-16): solo un plan de $0 tiene prueba. Si el cliente ya tiene una
    suscripción vigente a OTRO plan (p. ej. Free y compra Basic), se cambia a ese plan
    y nace `activa` con el período en curso pagado; antes se devolvía la Free sin tocar.
    Cada activación emite `subscription.activated` por el outbox.
    """
    precio = Decimal(str(plan.get("precio") or 0))
    trial_dias = int(plan.get("trial_dias") or 0) if precio == 0 else 0
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
        now = datetime.now(UTC)
        config = await _config()
        periodicidad = _periodicidad(plan.get("periodicidad"), config.periodicidad)
        if existente is not None:
            otra_variante = (
                variante_id is not None
                and existente.variante_id is not None
                and existente.variante_id != variante_id
            )
            if not otra_variante and Decimal(existente.valor_periodo or 0) == precio:
                return existente  # mismo plan: idempotente
            # Cambio de plan al comprar otro (Free → pago): activa con el período pagado.
            existente.variante_id = variante_id
            existente.concepto = plan.get("nombre") or existente.concepto
            existente.valor_periodo = precio
            existente.moneda = plan.get("moneda") or existente.moneda
            existente.periodicidad = periodicidad
            existente.estado = "trialing" if trial_dias else "activa"
            existente.trial_termina = (now + timedelta(days=trial_dias)) if trial_dias else None
            existente.dia_ancla = now.day
            existente.periodo_actual_inicio = now
            existente.proximo_cargo = existente.trial_termina or sumar_periodo(now, periodicidad, now.day)
            existente.motivo_pausa = None
            await session.flush()
            await _emitir_activada(session, existente, reference, cambio=True)
            return existente
        trial_termina = (now + timedelta(days=trial_dias)) if trial_dias else None
        sub = Subscription(
            tenant_id=get_current_tenant_id(),
            customer_id=customer_id,
            variante_id=variante_id,
            concepto=plan.get("nombre") or "Plan",
            estado="trialing" if trial_dias else "activa",
            valor_periodo=precio,
            moneda=plan.get("moneda") or "COP",
            periodicidad=periodicidad,
            dia_ancla=now.day,
            trial_termina=trial_termina,
            periodo_actual_inicio=now,
            # Con prueba, el primer cargo es al terminarla; sin prueba, el período
            # actual ya está pago (lo activó un pedido pagado) y el siguiente vence
            # por calendario (HU-SUB-04).
            proximo_cargo=trial_termina or sumar_periodo(now, periodicidad, now.day),
            fecha_alta=now,
        )
        session.add(sub)
        await session.flush()
        await _emitir_activada(session, sub, reference, cambio=False)
        return sub


async def _emitir_activada(session, sub: Subscription, reference: str, *, cambio: bool) -> None:
    from core.events.outbox import emit_domain
    from core.shared.events import ON_SUBSCRIPTION_ACTIVATED

    await emit_domain(
        session,
        tenant_id=sub.tenant_id,
        topic=ON_SUBSCRIPTION_ACTIVATED,
        dedupe_key=f"sub_activated:{sub.id}:{sub.variante_id or sub.concepto}",
        payload={
            "subscription_id": str(sub.id),
            "customer_id": str(sub.customer_id),
            "concepto": sub.concepto,
            "estado": sub.estado,
            "valor_periodo": str(sub.valor_periodo),
            "periodicidad": sub.periodicidad,
            "trial_termina": sub.trial_termina.isoformat() if sub.trial_termina else None,
            "referencia": reference,
            "cambio_de_plan": cambio,
        },
    )


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
            sub.motivo_pausa = "cliente"
            sub.proximo_cargo = None
            await session.flush()
            return _view(sub)

    async def resume(self, subscription_id: uuid.UUID, *, actor: str = "") -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.estado = "activa"
            sub.motivo_pausa = None
            sub.fecha_cancelacion = None
            if sub.proximo_cargo is None:
                sub.proximo_cargo = sumar_periodo(datetime.now(UTC), sub.periodicidad, sub.dia_ancla)
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
            sub.periodicidad = _periodicidad(plan.get("periodicidad"), sub.periodicidad)
            await session.flush()
            return _view(sub)

    async def charge_link(
        self, subscription_id: uuid.UUID, *, period: str = ""
    ) -> ChargeLink:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            ahora = datetime.now(UTC)
            if _en_prueba(sub, ahora):
                raise SubscriptionError(
                    "La suscripción está en período de prueba: todavía no hay nada que cobrar.",
                    code="en_prueba",
                )
            inicio_actual = sub.periodo_actual_inicio or ahora
            actual = etiqueta_periodo(inicio_actual, sub.periodicidad)
            if period and period > actual:
                raise SubscriptionError("No se cobra un período futuro.", code="periodo_futuro")
            filtro = [
                SubscriptionCharge.tenant_id == get_current_tenant_id(),
                SubscriptionCharge.subscription_id == subscription_id,
                SubscriptionCharge.deleted_at.is_(None),
            ]
            if period:
                filtro.append(SubscriptionCharge.periodo == period)
            # El cargo vencido más viejo, o el del período en curso: nunca uno futuro.
            cargo = (
                await session.execute(
                    select(SubscriptionCharge)
                    .where(*filtro, SubscriptionCharge.estado == "pendiente")
                    .order_by(SubscriptionCharge.fecha_vencimiento.asc().nulls_last())
                )
            ).scalars().first()
            periodo = cargo.periodo if cargo is not None else (period or actual)
            if cargo is None:
                pagado = (
                    await session.execute(
                        select(SubscriptionCharge.id).where(
                            *filtro, SubscriptionCharge.periodo == periodo, SubscriptionCharge.estado == "pagado"
                        )
                    )
                ).first()
                if pagado is not None:
                    raise SubscriptionError("Ese período ya está pago.", code="ya_pagado")
                cargo = SubscriptionCharge(
                    tenant_id=get_current_tenant_id(),
                    subscription_id=subscription_id,
                    periodo=periodo,
                    monto=sub.valor_periodo or 0,
                    estado="pendiente",
                    fecha_vencimiento=inicio_actual,
                )
                session.add(cargo)
                await session.flush()

        if cargo.link_url and cargo.referencia:
            # El cron ya generó el link de este cargo (HU-SUB-05): se reusa. Antes se pedía
            # otro con la misma referencia y la pasarela fallaba (journeys de HU-SUB-12).
            return {
                "url": cargo.link_url,
                "referencia": cargo.referencia,
                "periodo": periodo,
                "monto": float(cargo.monto or 0),
            }

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
                fila.link_url = link.url
                await session.flush()
        return {
            "url": link.url,
            "referencia": link.reference,
            "periodo": periodo,
            "monto": float(cargo.monto or 0),
        }

"""Servicio de suscripciones (ADR-0045).

Creación, lectura y transición de estado de la suscripción de un tenant. El
enforcement de runtime (¿puede operar? ¿le queda cuota?) vive en
`core.billing.enforcement`; acá están los helpers de datos.

`aplicar_pago_de_periodo` (HU-SH-04, ADR-0123) es la única puerta para aplicar un período
pagado a `tenant_subscriptions`, venga del asistente o del panel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.billing.periodos import Periodo, tiene_periodo_pago
from core.billing.plans import FREE_PLAN_ID, get_plan
from core.db.models import SubscriptionPayment, TenantSubscription, utcnow
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACTIVE_STATUSES = ("trialing", "active")

# Resultado de aplicar un pago de período (ADR-0123).
APLICADO = "aplicado"
YA_APLICADO = "ya_aplicado"
DUPLICADO = "duplicado"


def _trial_window(trial_days: int, now):
    return now + timedelta(days=trial_days)


async def ensure_free_subscription(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    now=None,
) -> TenantSubscription:
    """Garantiza una suscripción `free` para el tenant (idempotente).

    No hace commit: el llamador controla la transacción. Si ya existe una
    suscripción activa, la devuelve sin tocarla.
    """
    from datetime import UTC, datetime

    now = now or datetime.now(UTC)
    existing = (
        await session.execute(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == tenant_id,
                TenantSubscription.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    plan = get_plan(FREE_PLAN_ID)
    trial_days = plan.trial_days if plan else 30
    from core.config import get_settings

    trial_days = get_settings().billing_trial_days or trial_days
    trial_end = _trial_window(trial_days, now)
    sub = TenantSubscription(
        tenant_id=tenant_id,
        plan_id=FREE_PLAN_ID,
        status="trialing",
        period_start=now,
        period_end=trial_end,
        trial_ends_at=trial_end,
    )
    session.add(sub)
    await session.flush()
    log.info("free_subscription_created", tenant_id=str(tenant_id), trial_ends_at=trial_end.isoformat())
    return sub


async def get_active_subscription(tenant_id: uuid.UUID, *, session=None) -> TenantSubscription | None:
    """Suscripción activa del tenant (RLS-scoped). None si no hay ninguna.

    `session`: si el caller ya tiene una sesión abierta dentro de su propio
    `tenant_scope`, la pasa para no abrir una sesión anidada (mismo criterio
    que `core.billing.metrics.count_metric`).
    """
    if session is not None:
        return (
            await session.execute(
                select(TenantSubscription).where(
                    TenantSubscription.tenant_id == tenant_id,
                    TenantSubscription.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tenant_id,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()


@dataclass(frozen=True)
class PagoDePeriodo:
    """Qué pasó al aplicar el pago de un período (ADR-0123)."""

    resultado: str  # aplicado | ya_aplicado | duplicado
    referencia: str
    payment_id: uuid.UUID | None = None
    # Duplicado: ¿se registra por primera vez? (se alerta una sola vez por pago).
    nuevo: bool = True
    # Duplicado: proveedor del pago que ya cubría el período (`asistente`, `wompi`…).
    cubierto_por: str | None = None


async def _suscripcion_bloqueada(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> TenantSubscription:
    """La suscripción del negocio con `FOR UPDATE`: dos pagos del mismo negocio (el asistente
    en el worker y el panel en la API) se aplican de a uno, así el segundo ve al primero."""

    async def _leer() -> TenantSubscription | None:
        return (
            await session.execute(
                select(TenantSubscription)
                .where(TenantSubscription.tenant_id == tenant_id, TenantSubscription.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()

    sub = await _leer()
    if sub is None:
        await ensure_free_subscription(session, tenant_id, now=now)
        sub = await _leer()
    if sub is None:  # pragma: no cover - ensure_free_subscription la acaba de crear
        raise RuntimeError(f"El negocio {tenant_id} no tiene suscripción")
    return sub


async def aplicar_pago_de_periodo(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    plan_id: str,
    periodo: Periodo,
    provider: str,
    provider_reference: str,
    monto: Decimal | float | int | None = None,
    plan_en_el_acto: bool = True,
    dia_ancla: int | None = None,
    now: datetime | None = None,
) -> PagoDePeriodo:
    """Aplica a `tenant_subscriptions` el pago del período `periodo` (HU-SH-04, ADR-0123).

    Es la misma operación para los dos canales: el asistente (cargo de la suscripción del
    cliente en el negocio vendedor) y el panel (`/plan`, link de la plataforma).

    - **Idempotente** por `provider_reference`: el mismo pago aplicado dos veces no mueve nada.
    - **Un período, un pago:** si el período ya tiene un pago `aprobado` de OTRO pago, este
      queda `duplicado` (plata recibida dos veces: el llamador alerta) y la suscripción no se
      toca. El índice único parcial de `subscription_payments` lo garantiza en la base.
    - Los otros intentos pendientes del mismo período quedan `anulado`.
    - Desde Free, una prueba o una suscripción cancelada, el período pagado reemplaza al de la
      prueba (D-16: un plan pago no suma los días de la prueba).
    - Con un período pago en curso: extiende `period_end`; `period_start` se mueve solo si el
      período pagado es el que corre hoy (la ventana de las cuotas). El plan cambia en el acto
      si `plan_en_el_acto` o si el período ya empezó; si no, lo aplica el cron diario al
      empezar el período (`core/tasks/billing_lifecycle.py`).

    No hace commit: el llamador controla la transacción y corre bajo el `tenant_scope` del
    negocio.
    """
    now = now or datetime.now(UTC)
    plan = get_plan(plan_id)
    if plan is None or plan.is_free:
        raise ValueError(f"No es un plan pago: {plan_id!r}")
    referencia = periodo.referencia
    sub = await _suscripcion_bloqueada(session, tenant_id, now)

    previo = (
        await session.execute(
            select(SubscriptionPayment).where(
                SubscriptionPayment.tenant_id == tenant_id,
                SubscriptionPayment.periodo == referencia,
                SubscriptionPayment.status == "aprobado",
                SubscriptionPayment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    propio = (
        await session.execute(
            select(SubscriptionPayment).where(
                SubscriptionPayment.tenant_id == tenant_id,
                SubscriptionPayment.provider_reference == provider_reference,
                SubscriptionPayment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    importe = Decimal(str(monto)) if monto is not None else Decimal(plan.price_cop)

    # Un pago ya procesado (por su `provider_reference`) nunca se vuelve a procesar.
    if propio is not None and propio.status == "aprobado":
        return PagoDePeriodo(YA_APLICADO, propio.periodo or referencia, propio.id)
    if propio is not None and propio.status == DUPLICADO:
        cubierto = previo.provider if previo is not None else None
        return PagoDePeriodo(DUPLICADO, propio.periodo or referencia, propio.id, nuevo=False, cubierto_por=cubierto)

    if previo is not None:
        nuevo = True
        if propio is None:
            propio = SubscriptionPayment(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                subscription_id=sub.id,
                plan_id=plan.id,
                amount_cop=importe,
                currency=plan.currency,
                provider=provider,
                provider_reference=provider_reference,
            )
            session.add(propio)
        propio.status = DUPLICADO
        propio.provider = provider
        propio.periodo = referencia
        propio.period_start = periodo.inicio
        propio.period_end = periodo.fin
        propio.paid_at = propio.paid_at or now
        await session.flush()
        if nuevo:
            log.error(
                "cobro_plataforma_pago_duplicado",
                tenant_id=str(tenant_id),
                referencia=referencia,
                provider=provider,
                cubierto_por=previo.provider,
            )
        return PagoDePeriodo(DUPLICADO, referencia, propio.id, nuevo=nuevo, cubierto_por=previo.provider)

    if propio is None:
        propio = SubscriptionPayment(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            subscription_id=sub.id,
            currency=plan.currency,
            provider_reference=provider_reference,
        )
        session.add(propio)
    propio.plan_id = plan.id
    propio.amount_cop = importe
    propio.provider = provider
    propio.status = "aprobado"
    propio.periodo = referencia
    propio.period_start = periodo.inicio
    propio.period_end = periodo.fin
    propio.paid_at = now
    await session.flush()
    # Otros intentos del mismo período (un link del panel sin pagar, el cobro de la
    # renovación) ya no sirven: si alguien igual paga uno, quedará `duplicado`.
    await session.execute(
        update(SubscriptionPayment)
        .where(
            SubscriptionPayment.tenant_id == tenant_id,
            SubscriptionPayment.periodo == referencia,
            SubscriptionPayment.status == "pendiente",
            SubscriptionPayment.id != propio.id,
            SubscriptionPayment.deleted_at.is_(None),
        )
        .values(status="anulado", updated_at=utcnow())
    )

    plan_previo = sub.plan_id
    cubre_hoy = periodo.inicio <= now < periodo.fin
    if periodo.desde_cero or not tiene_periodo_pago(sub.plan_id, sub.status) or sub.period_end is None:
        sub.plan_id = plan.id
        sub.period_start = periodo.inicio
        sub.period_end = periodo.fin
        sub.dia_ancla = dia_ancla or periodo.inicio.day
    else:
        if plan_en_el_acto or cubre_hoy:
            sub.plan_id = plan.id
        if cubre_hoy:
            sub.period_start = periodo.inicio
        if periodo.inicio > sub.period_end:
            # Tramo nuevo después de un vencimiento largo: el calendario arranca de nuevo.
            sub.dia_ancla = dia_ancla or periodo.inicio.day
        else:
            sub.dia_ancla = dia_ancla or sub.dia_ancla or periodo.inicio.day
        if periodo.fin > sub.period_end:
            sub.period_end = periodo.fin
    if sub.period_end is not None and sub.period_end > now:
        sub.status = "active"
        sub.trial_ends_at = None
        sub.canceled_at = None
    sub.provider = provider
    sub.provider_reference = provider_reference
    sub.updated_at = utcnow()
    if sub.plan_id != plan_previo:
        # El plan cambió: los links pendientes de otro plan dejan de servir. Si alguien paga
        # uno igual, queda como pago doble y no devuelve el negocio al plan viejo.
        await session.execute(
            update(SubscriptionPayment)
            .where(
                SubscriptionPayment.tenant_id == tenant_id,
                SubscriptionPayment.status == "pendiente",
                SubscriptionPayment.plan_id != sub.plan_id,
                SubscriptionPayment.deleted_at.is_(None),
            )
            .values(status="anulado", updated_at=utcnow())
        )
    await session.flush()
    log.info(
        "cobro_plataforma_periodo_aplicado",
        tenant_id=str(tenant_id),
        referencia=referencia,
        plan=plan.id,
        provider=provider,
        period_end=sub.period_end.isoformat() if sub.period_end else None,
    )
    return PagoDePeriodo(APLICADO, referencia, propio.id)


async def registrar_periodo_pagado(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    plan_id: str,
    *,
    periodo: Periodo,
    dia_ancla: int | None = None,
    provider: str = "asistente",
    monto: Decimal | float | int | str | None = None,
) -> PagoDePeriodo:
    """Período pagado por el ASISTENTE de un negocio vendedor: el primero del alta automática
    (ADR-0121) o el de un cambio de Free a un plan pago (ADR-0123). La referencia del período
    es también la del pago: un reintento no lo vuelve a aplicar. Los límites del período
    salen de la suscripción del cliente en el vendedor, así los dos lados cortan el mismo día.
    """
    return await aplicar_pago_de_periodo(
        session,
        tenant_id,
        plan_id=plan_id,
        periodo=periodo,
        provider=provider,
        provider_reference=periodo.referencia,
        plan_en_el_acto=True,
        dia_ancla=dia_ancla,
        monto=monto,
    )

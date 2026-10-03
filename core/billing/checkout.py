"""Inicio y confirmación de la suscripción de un tenant a un plan pago (ADR-0046).

- `start_subscription(plan_id)`: crea el `subscription_payments` (pendiente) y el
  link de pago del proveedor; devuelve la URL de checkout.
- `apply_payment(provider_reference, status, ...)`: idempotente, invocado por el
  webhook de billing cuando el pago se aprueba/rechaza. Al aprobar: extiende la
  suscripción y registra el período cubierto.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.billing.plans import get_plan
from core.billing.providers import get_billing_provider
from core.billing.providers.base import BillingRequestRejected
from core.db.models import SubscriptionPayment, TenantSubscription, utcnow
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)


class BillingNotConfigured(Exception):
    """La pasarela de pago no tiene credenciales (BILLING_PROVIDER=none)."""


async def start_subscription(plan_id: str) -> str | None:
    """Crea el pago pendiente + link de checkout. Devuelve la URL o None."""
    plan = get_plan(plan_id)
    if plan is None:
        raise BillingRequestRejected(f"Plan desconocido: {plan_id}")
    if plan.is_free:
        raise BillingRequestRejected("El plan free no requiere pago")
    provider = get_billing_provider()
    if provider is None:
        return None

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise BillingNotConfigured("Sin tenant en contexto")

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tenant_id,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if sub is None:
                from core.billing.service import ensure_free_subscription

                sub = await ensure_free_subscription(session, tenant_id)
            sub_id = sub.id
            reference = f"sub-{tenant_id.hex[:8]}-{uuid.uuid4().hex[:10]}"
            await session.commit()

    # El link se crea ANTES de persistir el pago: si la red falla no queda una
    # fila "pendiente" huérfana; si el link existe, el webhook siempre encontrará
    # la fila por `provider_reference` (evita cobro sin activación).
    link = await provider.create_payment_link(
        reference=reference, amount_cop=plan.price_cop, plan_name=plan.name
    )

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            payment = SubscriptionPayment(
                tenant_id=tenant_id,
                subscription_id=sub_id,
                plan_id=plan.id,
                amount_cop=plan.price_cop,
                currency=plan.currency,
                status="pendiente",
                provider=provider.provider_name,
                provider_reference=link.provider_reference,
                checkout_url=link.url,
            )
            session.add(payment)
            await session.commit()
    log.info("subscription_checkout_started", tenant_id=str(tenant_id), plan=plan.id)
    return link.url


async def apply_payment(
    *,
    provider_reference: str,
    status: str,
    tenant_id: uuid.UUID | None = None,
) -> bool:
    """Aplica un evento de pago (idempotente). True si actualizó algo.

    Correlaciona por `provider_reference` (el id del link). Si ya estaba
    `aprobado`, no repite la extensión (reintento del webhook).
    """
    if not provider_reference:
        return False

    async def _apply(tid: uuid.UUID) -> bool:
        async with tenant_scope(tid):
            async with get_session() as session:
                payment = (
                    await session.execute(
                        select(SubscriptionPayment).where(
                            SubscriptionPayment.tenant_id == tid,
                            SubscriptionPayment.provider_reference == provider_reference,
                            SubscriptionPayment.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if payment is None:
                    return False
                if payment.status == "aprobado" and status == "aprobado":
                    return False  # ya aplicado: idempotente

                payment.status = status
                if status != "aprobado":
                    if status == "rechazado":
                        await session.commit()
                    return status == "rechazado"

                plan = get_plan(payment.plan_id)
                interval_days = plan.interval_days if plan else 30
                now = datetime.now(UTC)

                sub = await session.get(TenantSubscription, payment.subscription_id)
                if sub is not None:
                    # Extiende desde el fin del período actual si todavía no
                    # venció (renovación anticipada), o desde ahora.
                    base = sub.period_end if sub.period_end and sub.period_end > now else now
                    sub.plan_id = payment.plan_id
                    sub.status = "active"
                    sub.period_start = now
                    sub.period_end = base + timedelta(days=interval_days)
                    sub.trial_ends_at = None
                    sub.provider = payment.provider
                    sub.provider_reference = provider_reference
                    sub.updated_at = utcnow()
                payment.paid_at = now
                payment.period_start = now
                payment.period_end = (base + timedelta(days=interval_days)) if sub else None
                await session.commit()
                log.info(
                    "subscription_activated",
                    tenant_id=str(tid),
                    plan=payment.plan_id,
                    period_end=payment.period_end.isoformat() if payment.period_end else None,
                )
                return True

    if tenant_id is not None:
        return await _apply(tenant_id)

    # Sin tenant explícito (webhook de sistema): resolver el tenant del pago con
    # una función SECURITY DEFINER (una sola query, sin enumerar tenants). La
    # función SOLO devuelve el tenant_id, nunca el contenido del pago.
    from sqlalchemy import text

    async with get_session() as session:
        tid = (
            await session.execute(
                text("select resolve_subscription_payment_tenant(:ref)"),
                {"ref": provider_reference},
            )
        ).scalar_one_or_none()
    if tid is None:
        return False
    return await _apply(tid)

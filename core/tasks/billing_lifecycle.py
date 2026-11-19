"""Normalización del ciclo de vida de las suscripciones (ADR-0045).

El enforcement se calcula EN VIVO (no depende de este cron): el webhook y el
consumer bloquean un tenant con el trial/período vencido aunque este job no
corra. El cron solo mantiene el `status` de la suscripción al día para que el
panel muestre el estado correcto y para que las transiciones queden registradas.

- `trialing` con `trial_ends_at` vencido → `expired`.
- `active` con `period_end` vencido → `past_due`.
- HU-SH-04 (ADR-0123): cuando empieza un período que se pagó por adelantado, la suscripción
  pasa a ese período: `period_start` (la ventana de las cuotas) y su plan, si el pago traía
  otro (p. ej. un downgrade programado en el asistente).

La renovación (el cobro del período siguiente) es `core/billing/renovacion.py`, y la
conciliación de los dos canales, `core/billing/conciliacion.py`.

No suspende el tenant ni borra nada: eso es billing (operacional), no el ciclo
de vida `active|suspended` de `docs/MULTITENANT.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.db.models import SubscriptionPayment, Tenant, TenantSubscription
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


async def normalize_expired_subscriptions() -> dict[str, int]:
    """Transiciona a `expired`/`past_due` toda suscripción vencida y pasa al período que
    empezó hoy si estaba pagado por adelantado.

    Enumera los tenants vivos (tabla de sistema, sin RLS) y entra a cada uno con
    su propio `tenant_scope`, porque `tenant_subscriptions` sí tiene RLS.
    """
    from sqlalchemy import select

    now = datetime.now(UTC)
    counts = {"expired": 0, "past_due": 0, "periodo_nuevo": 0}

    async with get_session() as session:
        tenant_ids = (
            await session.execute(
                select(Tenant.id).where(Tenant.deleted_at.is_(None))
            )
        ).scalars().all()

    for tenant_id in tenant_ids:
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
                    continue
                if sub.status == "trialing":
                    deadline = sub.trial_ends_at or sub.period_end
                    if deadline is not None and deadline < now:
                        sub.status = "expired"
                        counts["expired"] += 1
                elif sub.status == "active":
                    if sub.period_end is not None and sub.period_end < now:
                        sub.status = "past_due"
                        counts["past_due"] += 1
                    else:
                        vigente = (
                            await session.execute(
                                select(SubscriptionPayment)
                                .where(
                                    SubscriptionPayment.tenant_id == tenant_id,
                                    SubscriptionPayment.status == "aprobado",
                                    SubscriptionPayment.periodo.isnot(None),
                                    SubscriptionPayment.period_start <= now,
                                    SubscriptionPayment.period_end > now,
                                    SubscriptionPayment.deleted_at.is_(None),
                                )
                                .order_by(SubscriptionPayment.period_start.desc())
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        if vigente is not None and vigente.period_start is not None and (
                            sub.period_start is None or vigente.period_start > sub.period_start
                        ):
                            sub.period_start = vigente.period_start
                            sub.plan_id = vigente.plan_id
                            counts["periodo_nuevo"] += 1

    if any(counts.values()):
        log.info("subscriptions_normalized", **counts)
    return counts

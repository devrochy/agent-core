"""Normalización del ciclo de vida de las suscripciones (ADR-0045).

El enforcement se calcula EN VIVO (no depende de este cron): el webhook y el
consumer bloquean un tenant con el trial/período vencido aunque este job no
corra. El cron solo mantiene el `status` de la suscripción al día para que el
panel muestre el estado correcto y para que las transiciones queden registradas.

- `trialing` con `trial_ends_at` vencido → `expired`.
- `active` con `period_end` vencido → `past_due`.

No suspende el tenant ni borra nada: eso es billing (operacional), no el ciclo
de vida `active|suspended` de `docs/MULTITENANT.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.db.models import Tenant, TenantSubscription
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


async def normalize_expired_subscriptions() -> dict[str, int]:
    """Transiciona a `expired`/`past_due` toda suscripción vencida.

    Enumera los tenants vivos (tabla de sistema, sin RLS) y entra a cada uno con
    su propio `tenant_scope`, porque `tenant_subscriptions` sí tiene RLS.
    """
    from sqlalchemy import select

    now = datetime.now(UTC)
    counts = {"expired": 0, "past_due": 0}

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

    if counts["expired"] or counts["past_due"]:
        log.info("subscriptions_normalized", **counts)
    return counts

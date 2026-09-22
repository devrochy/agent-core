"""Servicio de suscripciones (ADR-0045).

Creación, lectura y transición de estado de la suscripción de un tenant. El
enforcement de runtime (¿puede operar? ¿le queda cuota?) vive en
`core.billing.enforcement`; acá están los helpers de datos.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.billing.plans import FREE_PLAN_ID, get_plan
from core.db.models import TenantSubscription
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACTIVE_STATUSES = ("trialing", "active")


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

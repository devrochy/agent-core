"""Datos de plan/consumo para el panel (ADR-0045).

Aísla las consultas de billing de las vistas: `views.py` (página `/admin/plan` y
dashboard) y las plantillas consumen estas funciones. Todo es tolerante a fallos:
si falta el schema (migración no aplicada) o la suscripción, devuelve estructuras
vacías en vez de tumbar el panel.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.billing.enforcement import SubscriptionOverview
from core.billing.metrics import usage_snapshot
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


async def subscription_overview_or_none(tenant_id: uuid.UUID) -> SubscriptionOverview | None:
    from core.billing.enforcement import subscription_overview

    try:
        return await subscription_overview(tenant_id)
    except Exception as exc:  # schema ausente antes de migrar: no tumbar el panel
        log.warning("billing_overview_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
        return None


async def billing_usage(
    tenant_id: uuid.UUID, overview: SubscriptionOverview | None
) -> list[dict[str, Any]]:
    """Consumo + límite por métrica (barras de progreso). [] si no hay plan."""
    if overview is None or not overview.limits:
        return []
    try:
        return await usage_snapshot(
            tenant_id, period_start=overview.period_start, limits=overview.limits
        )
    except Exception as exc:
        log.warning("billing_usage_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
        return []


async def billing_payments(tenant_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
    """Historial de pagos del plan (más recientes primero)."""
    from core.db.models import SubscriptionPayment

    try:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(SubscriptionPayment)
                        .where(
                            SubscriptionPayment.tenant_id == tenant_id,
                            SubscriptionPayment.deleted_at.is_(None),
                        )
                        .order_by(SubscriptionPayment.created_at.desc())
                        .limit(limit)
                    )
                ).scalars().all()
    except Exception as exc:
        log.warning("billing_payments_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
        return []
    return [
        {
            "plan_id": p.plan_id,
            "amount_cop": float(p.amount_cop),
            "status": p.status,
            "provider": p.provider,
            "created_at": p.created_at,
            "paid_at": p.paid_at,
            "checkout_url": p.checkout_url,
        }
        for p in rows
    ]


async def dashboard_billing_summary(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Resumen compacto para el dashboard: plan, días restantes, alerta de cuota."""
    overview = await subscription_overview_or_none(tenant_id)
    usage = await billing_usage(tenant_id, overview)
    alert = next((u for u in usage if u["exceeded"]), None)
    warning = next((u for u in usage if u["percent"] is not None and u["percent"] >= 80), None)
    return {
        "plan_name": overview.plan_name if overview else "—",
        "status": overview.status if overview else "sin_suscripcion",
        "operational": overview.operational if overview else True,
        "days_left": overview.days_left if overview else None,
        "alert_metric": alert,
        "warning_metric": warning,
        "has_plan": overview is not None and overview.plan_id is not None,
    }

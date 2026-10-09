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
            "periodo": p.periodo,
        }
        for p in rows
    ]


async def cobro_del_periodo(tenant_id: uuid.UUID) -> tuple[dict[str, Any] | None, bool]:
    """El período que toca pagar del plan actual y si el plan se cambia por el asistente
    (HU-SH-04, ADR-0123). (None, …) para Free o sin suscripción: ahí se elige un plan."""
    from datetime import UTC, datetime, timedelta

    from core.billing.canales import vinculo_asistente
    from core.billing.periodos import periodo_a_cobrar, referencia_del_periodo
    from core.billing.plans import get_plan
    from core.config import get_settings
    from core.db.models import SubscriptionPayment, Tenant, TenantSubscription

    try:
        por_el_asistente = await vinculo_asistente(tenant_id) is not None
        now = datetime.now(UTC)
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                sub = (
                    await session.execute(
                        select(TenantSubscription).where(
                            TenantSubscription.tenant_id == tenant_id, TenantSubscription.deleted_at.is_(None)
                        )
                    )
                ).scalar_one_or_none()
                plan = get_plan(sub.plan_id) if sub is not None else None
                tenant = await session.get(Tenant, tenant_id)
                if sub is None or plan is None or plan.is_free or tenant is None:
                    return None, por_el_asistente
                periodo = periodo_a_cobrar(
                    slug=tenant.slug,
                    plan_id=sub.plan_id,
                    status=sub.status,
                    period_end=sub.period_end,
                    dia_ancla=sub.dia_ancla,
                    now=now,
                )
                pagos = (
                    await session.execute(
                        select(SubscriptionPayment)
                        .where(
                            SubscriptionPayment.tenant_id == tenant_id,
                            SubscriptionPayment.status == "aprobado",
                            SubscriptionPayment.periodo.isnot(None),
                            SubscriptionPayment.deleted_at.is_(None),
                        )
                        .order_by(SubscriptionPayment.period_start.desc())
                        .limit(2)
                    )
                ).scalars().all()
    except Exception as exc:  # no tumbar el panel (mismo criterio que el resto de esta vista)
        log.warning("billing_cobro_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
        return None, False

    def _canal(provider: str | None) -> str:
        return "asistente" if provider == "asistente" else "panel"

    ventana = timedelta(days=get_settings().billing_dias_renovacion)
    if not periodo.desde_cero and periodo.inicio > now + ventana:
        # Al día: se muestra el último período pagado; el siguiente todavía no se cobra
        # (pagar respondería «ya pagado», igual que `start_subscription`).
        ultimo = pagos[0] if pagos else None
        return (
            {
                "referencia": (ultimo.periodo if ultimo else None) or referencia_del_periodo(tenant.slug, sub.period_start),
                "inicio": ultimo.period_start if ultimo else sub.period_start,
                "fin": ultimo.period_end if ultimo else sub.period_end,
                "plan_id": ultimo.plan_id if ultimo else plan.id,
                "monto_cop": int(ultimo.amount_cop) if ultimo else plan.price_cop,
                "estado": "pagado",
                "pagado_por": _canal(ultimo.provider) if ultimo else None,
                "disponible_desde": periodo.inicio - ventana,
            },
            por_el_asistente,
        )
    pagado = next((p for p in pagos if p.periodo == periodo.referencia), None)
    return (
        {
            "referencia": periodo.referencia,
            "inicio": periodo.inicio,
            "fin": periodo.fin,
            "plan_id": plan.id,
            "monto_cop": plan.price_cop,
            "estado": "pagado" if pagado else "pendiente",
            "pagado_por": _canal(pagado.provider) if pagado else None,
            "disponible_desde": None,
        },
        por_el_asistente,
    )


async def dashboard_billing_summary(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Resumen compacto para el dashboard: plan, días restantes, alerta de cuota."""
    overview = await subscription_overview_or_none(tenant_id)
    usage = await billing_usage(tenant_id, overview)
    # Rojo: SUPERÓ el límite (no basta con estar en el tope).
    alert = next((u for u in usage if u["exceeded"]), None)
    # Ámbar: dependiente de los usos RESTANTES -- avisa cuando queda poco margen
    # (<= 1 uso, o <= 20% del límite), nunca si ya no queda ninguno (0 = tope o
    # excedido). `remaining` se adjunta para que el banner sea accionable.
    warning = None
    for u in usage:
        if u["limit"] is None or u["exceeded"]:
            continue
        remaining = u["limit"] - u["used"]
        if remaining > 0 and (remaining <= 1 or remaining <= u["limit"] * 0.2):
            warning = {**u, "remaining": remaining}
            break
    return {
        "plan_name": overview.plan_name if overview else "—",
        "status": overview.status if overview else "sin_suscripcion",
        "operational": overview.operational if overview else True,
        "days_left": overview.days_left if overview else None,
        "alert_metric": alert,
        "warning_metric": warning,
        "has_plan": overview is not None and overview.plan_id is not None,
    }

"""Plan y consumo (lectura): `GET /api/v1/admin/plan`.

Plan activo, consumo por métrica, historial de pagos y catálogo de planes.
Mismos datos que la vista SSR `/admin/plan` (ADR-0045).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.admin.plan_view import (
    billing_payments,
    billing_usage,
    subscription_overview_or_none,
)
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.schemas import UsageMetric
from core.shared.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/admin/plan", tags=["admin · plan"])


class PlanInfo(BaseModel):
    id: str
    name: str
    price_cop: int
    is_free: bool
    current: bool
    limits: dict[str, int | None]
    features: dict[str, Any]


class PlanOverview(BaseModel):
    plan_id: str | None
    plan_name: str
    status: str
    operational: bool
    days_left: int | None
    has_plan: bool


class Payment(BaseModel):
    plan_id: str | None
    amount_cop: float
    status: str
    provider: str | None
    created_at: datetime
    paid_at: datetime | None
    checkout_url: str | None


class PlanResponse(BaseModel):
    overview: PlanOverview | None
    usage: list[UsageMetric]
    payments: list[Payment]
    plans: list[PlanInfo]


@router.get("", response_model=PlanResponse)
async def get_plan(
    principal: Annotated[Principal, Depends(require("plan"))],
) -> PlanResponse:
    from core.billing.plans import public_plans

    tenant_id = principal.tenant_id
    overview = await subscription_overview_or_none(tenant_id) if tenant_id else None
    usage = await billing_usage(tenant_id, overview) if tenant_id else []
    payments = await billing_payments(tenant_id) if tenant_id else []

    current_plan_id = overview.plan_id if overview else None
    return PlanResponse(
        overview=(
            PlanOverview(
                plan_id=overview.plan_id,
                plan_name=overview.plan_name,
                status=overview.status,
                operational=overview.operational,
                days_left=overview.days_left,
                has_plan=overview.plan_id is not None,
            )
            if overview
            else None
        ),
        usage=[UsageMetric(**u) for u in usage],
        payments=[Payment(**p) for p in payments],
        plans=[
            PlanInfo(
                id=plan.id,
                name=plan.name,
                price_cop=plan.price_cop,
                is_free=plan.is_free,
                current=plan.id == current_plan_id,
                limits=plan.limits,
                features=plan.features,
            )
            for plan in public_plans()
        ],
    )


class SubscribeRequest(BaseModel):
    plan_id: str


class SubscribeResponse(BaseModel):
    checkout_url: str


@router.post("/subscribe", response_model=SubscribeResponse)
async def subscribe_to_plan(
    payload: SubscribeRequest,
    principal: Annotated[Principal, Depends(require("plan", write=True))],
) -> SubscribeResponse:
    """Inicia la suscripción a un plan pago: devuelve la URL del checkout."""
    from core.billing.checkout import start_subscription
    from core.billing.plans import get_plan

    if get_plan(payload.plan_id) is None:
        raise ApiError.bad_request("Plan desconocido.", "unknown_plan")

    try:
        checkout_url = await start_subscription(payload.plan_id)
    except Exception as exc:
        log.exception("plan_subscribe_failed", plan=payload.plan_id)
        raise ApiError(502, "checkout_failed", f"No se pudo iniciar el pago: {str(exc)[:140]}") from exc
    if checkout_url is None:
        raise ApiError.bad_request(
            "La pasarela de pago no está configurada.", "checkout_unavailable"
        )
    return SubscribeResponse(checkout_url=checkout_url)

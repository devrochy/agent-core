"""Plan y consumo (lectura): `GET /api/v1/admin/plan`.

Plan activo, consumo por métrica, historial de pagos y catálogo de planes. Desde HU-BILL-05
(D-35, ADR-0142) el consumo trae `agentes` y `colaboradores`, y `credit` el crédito mensual.
Mismos datos que la vista SSR `/admin/plan` (ADR-0045).

Desde HU-SH-04 (ADR-0123) es también el **canal panel** del cobro de la plataforma: muestra el
período que toca pagar (`cobro`) con su referencia, si ya lo pagó el asistente, y
`POST /subscribe` responde 409 `ya_pagado` en vez de cobrar dos veces el mismo período.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.admin.plan_view import (
    billing_payments,
    billing_usage,
    cobro_del_periodo,
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
    # Crédito mensual de uso del plan (D-35; 1 crédito = COP 10).
    monthly_credits: int | None = None


class CreditUsage(BaseModel):
    """Crédito mensual del plan (D-35, ADR-0142). `used`/`percent` son `null` mientras el core no
    mide el consumo en créditos (ledger multiconcepto, HU-BILL-04/06): el panel muestra el tope
    y la barra vacía con «medición pendiente»."""

    limit: int
    used: int | None = None
    percent: float | None = None


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
    # HU-SH-04: referencia del período que cubre (`plat-<slug>-<AAAAMM>`).
    periodo: str | None = None


class Cobro(BaseModel):
    """El período que toca pagar del plan actual (HU-SH-04, ADR-0123)."""

    referencia: str
    inicio: datetime
    fin: datetime
    plan_id: str
    monto_cop: int
    estado: str  # pendiente | pagado
    pagado_por: str | None = None  # asistente | panel
    # Al día: desde cuándo se puede pagar el período siguiente.
    disponible_desde: datetime | None = None


class PlanResponse(BaseModel):
    overview: PlanOverview | None
    usage: list[UsageMetric]
    payments: list[Payment]
    plans: list[PlanInfo]
    cobro: Cobro | None = None
    # Crédito mensual del plan actual (null sin plan o si el plan no define crédito).
    credit: CreditUsage | None = None
    # El negocio lo creó el asistente de SolversHub: su plan se cambia por el asistente.
    plan_por_el_asistente: bool = False


@router.get("", response_model=PlanResponse)
async def get_plan(
    principal: Annotated[Principal, Depends(require("plan"))],
) -> PlanResponse:
    from core.billing.plans import get_plan as plan_del_catalogo
    from core.billing.plans import public_plans

    tenant_id = principal.tenant_id
    overview = await subscription_overview_or_none(tenant_id) if tenant_id else None
    usage = await billing_usage(tenant_id, overview) if tenant_id else []
    payments = await billing_payments(tenant_id) if tenant_id else []
    cobro, por_el_asistente = await cobro_del_periodo(tenant_id) if tenant_id else (None, False)

    current_plan_id = overview.plan_id if overview else None
    plan_actual = plan_del_catalogo(current_plan_id) if current_plan_id else None
    credito = (
        CreditUsage(limit=plan_actual.monthly_credits)
        if plan_actual is not None and plan_actual.monthly_credits is not None
        else None
    )
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
                monthly_credits=plan.monthly_credits,
            )
            for plan in public_plans()
        ],
        cobro=Cobro(**cobro) if cobro else None,
        credit=credito,
        plan_por_el_asistente=por_el_asistente,
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
    """Link de pago del período que toca pagar (plan actual) o de un plan nuevo.

    409 `ya_pagado`: ese período ya está pagado (por el panel o por el asistente).
    409 `cambio_en_el_asistente`: el negocio lo creó el asistente; su plan se cambia ahí."""
    from core.billing.checkout import CambioDePlanEnElAsistente, PeriodoYaPagado, start_subscription
    from core.billing.plans import get_plan

    if get_plan(payload.plan_id) is None:
        raise ApiError.bad_request("Plan desconocido.", "unknown_plan")

    try:
        checkout_url = await start_subscription(payload.plan_id)
    except (PeriodoYaPagado, CambioDePlanEnElAsistente) as exc:
        raise ApiError(409, exc.code, str(exc)) from exc
    except Exception as exc:
        log.exception("plan_subscribe_failed", plan=payload.plan_id)
        # El detalle (respuesta del proveedor, ids) va al log, no al cliente.
        raise ApiError(502, "checkout_failed", "No se pudo iniciar el pago. Intenta de nuevo en unos minutos.") from exc
    if checkout_url is None:
        raise ApiError.bad_request(
            "La pasarela de pago no está configurada.", "checkout_unavailable"
        )
    return SubscribeResponse(checkout_url=checkout_url)

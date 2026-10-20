"""Costos LLM (lectura): `GET /api/v1/admin/costs`.

Mismos agregados que la vista SSR `/admin/costos` (parte LLM): gasto de hoy y de
30 días, turnos totales, desglose por modelo y por conversación.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from core.admin.meta_costs_service import (
    list_credit_connections,
    meta_costs_context,
    sync_meta_pricing,
    verify_credits,
)
from core.api.deps import Principal, require
from core.db.models import UsageLedger
from core.db.session import get_session

router = APIRouter(prefix="/admin/costs", tags=["admin · costos"])


class ModelCost(BaseModel):
    model: str
    calls: int
    tokens_in: int
    tokens_out: int
    avg_latency: int
    cost: float


class ConversationCost(BaseModel):
    conversation_id: str
    turns: int
    cost: float
    last: datetime | None


class MetaCategoryCost(BaseModel):
    category: str
    country: str
    volume: int
    cost: float


class MetaPhoneCost(BaseModel):
    phone_number: str
    volume: int
    cost: float


class MetaDiscrepancy(BaseModel):
    category: str
    webhook_count: int
    pricing_volume: int
    diff: int


class MetaPricing(BaseModel):
    by_category_country: list[MetaCategoryCost] = []
    by_phone: list[MetaPhoneCost] = []
    discrepancy: list[MetaDiscrepancy] = []
    service_allowance_hypothesis: bool = False


class CostsResponse(BaseModel):
    meta: MetaPricing | None = None
    today: float
    month: float
    total_turns: int
    by_model: list[ModelCost]
    by_conversation: list[ConversationCost]


@router.get("", response_model=CostsResponse)
async def get_costs(
    principal: Annotated[Principal, Depends(require("costs"))],
) -> CostsResponse:
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_ago = now - timedelta(days=30)
    by_tenant = UsageLedger.tenant_id == principal.tenant_id

    async with get_session() as session:
        today_cost = (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    by_tenant, UsageLedger.created_at >= today_start
                )
            )
        ).scalar() or 0
        month_cost = (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    by_tenant, UsageLedger.created_at >= month_ago
                )
            )
        ).scalar() or 0
        # Las filas `embedding` (ingesta del pack de conocimiento, ADR-0145 §3) no son turnos.
        total_turns = (
            await session.execute(
                select(func.count(UsageLedger.id)).where(by_tenant, UsageLedger.kind != "embedding")
            )
        ).scalar() or 0
        by_model_rows = (
            await session.execute(
                select(
                    UsageLedger.model,
                    func.count(UsageLedger.id),
                    func.sum(UsageLedger.input_tokens),
                    func.sum(UsageLedger.output_tokens),
                    func.avg(UsageLedger.latency_ms),
                    func.sum(UsageLedger.cost_usd),
                )
                .where(by_tenant, UsageLedger.created_at >= month_ago)
                .group_by(UsageLedger.model)
            )
        ).all()
        by_conversation_rows = (
            await session.execute(
                select(
                    UsageLedger.conversation_id,
                    func.count(UsageLedger.id),
                    func.sum(UsageLedger.cost_usd),
                    func.max(UsageLedger.created_at),
                )
                .where(by_tenant, UsageLedger.created_at >= month_ago, UsageLedger.kind != "embedding")
                .group_by(UsageLedger.conversation_id)
                .order_by(func.sum(UsageLedger.cost_usd).desc())
                .limit(20)
            )
        ).all()

    meta_ctx = (
        await meta_costs_context(principal.tenant_id, month_ago)
        if principal.tenant_id is not None
        else {}
    )
    meta = MetaPricing(
        by_category_country=[MetaCategoryCost(**r) for r in meta_ctx.get('meta_by_category_country', [])],
        by_phone=[MetaPhoneCost(**r) for r in meta_ctx.get('meta_by_phone', [])],
        discrepancy=[MetaDiscrepancy(**r) for r in meta_ctx.get('meta_discrepancy', [])],
        service_allowance_hypothesis=bool(
            meta_ctx.get('meta_service_allowance_hypothesis_enabled', False)
        ),
    )
    return CostsResponse(
        meta=meta,
        today=float(today_cost),
        month=float(month_cost),
        total_turns=int(total_turns),
        by_model=[
            ModelCost(
                model=model,
                calls=int(calls),
                tokens_in=int(tokens_in or 0),
                tokens_out=int(tokens_out or 0),
                avg_latency=int(avg_latency or 0),
                cost=float(cost or 0),
            )
            for model, calls, tokens_in, tokens_out, avg_latency, cost in by_model_rows
        ],
        by_conversation=[
            ConversationCost(
                conversation_id=str(conversation_id)[:8],
                turns=int(turns),
                cost=float(cost or 0),
                last=last,
            )
            for conversation_id, turns, cost, last in by_conversation_rows
        ],
    )


# ------------------------------------------------------------------ Meta (WhatsApp)
class MetaSyncResult(BaseModel):
    errors: list[str]


class MetaCreditConnection(BaseModel):
    id: uuid.UUID
    label: str | None
    channel: str
    business_id: str | None


class MetaCreditsResponse(BaseModel):
    connections: list[MetaCreditConnection]
    credits: list[dict[str, Any]] = []
    error: str | None = None


def _credit_connections(rows) -> list[MetaCreditConnection]:
    # `MetaConnection` no tiene columna `label` (N-18): se usa `meta_app_id`
    # (o el business id) como nombre visible, manteniendo el campo `label` del
    # contrato JSON que ya consume el panel.
    return [
        MetaCreditConnection(
            id=c.id,
            label=c.meta_app_id or c.business_id,
            channel=c.channel,
            business_id=c.business_id,
        )
        for c in rows
    ]


@router.post("/meta/sync", response_model=MetaSyncResult)
async def sync_costs_meta(
    principal: Annotated[Principal, Depends(require("costs", write=True))],
) -> MetaSyncResult:
    """Dispara el mismo trabajo que el cron diario de precios de Meta."""
    errors = await sync_meta_pricing(principal.tenant_id)
    return MetaSyncResult(errors=errors)


@router.get("/meta/credits", response_model=MetaCreditsResponse)
async def get_costs_meta_credits(
    principal: Annotated[Principal, Depends(require("costs"))],
) -> MetaCreditsResponse:
    """Conexiones de Meta del tenant candidatas a consultar créditos."""
    rows = await list_credit_connections(principal.tenant_id)
    return MetaCreditsResponse(connections=_credit_connections(rows))


@router.post("/meta/credits/{connection_id}/verify", response_model=MetaCreditsResponse)
async def verify_costs_meta_credits(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("costs", write=True))],
) -> MetaCreditsResponse:
    """Consulta (solo lectura) las líneas de crédito extendidas de una conexión."""
    rows = await list_credit_connections(principal.tenant_id)
    credits, error = await verify_credits(principal.tenant_id, connection_id)
    return MetaCreditsResponse(
        connections=_credit_connections(rows),
        credits=[dict(c) for c in credits],
        error=error,
    )

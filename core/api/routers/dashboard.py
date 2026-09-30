"""Dashboard del panel: `GET /api/v1/admin/dashboard`.

Mismos datos que la vista SSR `/admin` (`core/admin/templates/dashboard.html`):
KPIs de 7/30 días, estado del bot, LLM activo, packs habilitados, resumen de plan
y conversaciones recientes. Requiere la sección `dashboard`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from core.admin.plan_view import dashboard_billing_summary
from core.agents.spec import count_tenant_packs, load_active_spec
from core.api.deps import Principal, require
from core.api.schemas import UsageMetric
from core.config import get_settings
from core.db.models import (
    AppSettings,
    Conversation,
    Customer,
    Escalation,
    Message,
    UsageLedger,
)
from core.db.session import get_session
from core.shared.test_mode import TEST_CHANNEL

router = APIRouter(prefix="/admin", tags=["admin · dashboard"])


class BillingSummary(BaseModel):
    plan_name: str
    status: str
    operational: bool
    days_left: int | None
    has_plan: bool
    alert_metric: UsageMetric | None = None
    warning_metric: UsageMetric | None = None


class Kpi(BaseModel):
    conversations_7d: int
    messages_7d: int
    pending_escalations: int
    cost_30d: float
    active_plugins: int


class RecentConversation(BaseModel):
    id: uuid.UUID
    customer_name: str | None
    external_id: str | None
    status: str


class DashboardResponse(BaseModel):
    tenant_id: uuid.UUID | None
    kpi: Kpi
    bot_paused: bool
    llm_label: str
    recent: list[RecentConversation]
    billing: BillingSummary | None


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    principal: Annotated[Principal, Depends(require("dashboard"))],
) -> DashboardResponse:
    tenant_id = principal.tenant_id
    week_ago = datetime.now(UTC) - timedelta(days=7)
    month_ago = datetime.now(UTC) - timedelta(days=30)

    async with get_session() as session:
        conversations_7d = (
            await session.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.started_at >= week_ago,
                    Conversation.channel != TEST_CHANNEL,
                )
            )
        ).scalar() or 0
        messages_7d = (
            await session.execute(
                select(func.count(Message.id)).where(
                    Message.tenant_id == tenant_id,
                    Message.created_at >= week_ago,
                )
            )
        ).scalar() or 0
        pending = (
            await session.execute(
                select(func.count(Escalation.id)).where(
                    Escalation.tenant_id == tenant_id,
                    Escalation.status == "pendiente",
                )
            )
        ).scalar() or 0
        cost_30d = (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.created_at >= month_ago,
                )
            )
        ).scalar() or 0
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        recent_rows = (
            await session.execute(
                select(Conversation, Customer)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.channel != TEST_CHANNEL,
                )
                .order_by(Conversation.started_at.desc())
                .limit(8)
            )
        ).all()

    settings = get_settings()
    llm_provider = (app_settings.llm_provider if app_settings else None) or settings.llm_provider
    llm_model = (app_settings.llm_model if app_settings else None) or settings.llm_model

    billing = await dashboard_billing_summary(tenant_id) if tenant_id else None

    return DashboardResponse(
        tenant_id=tenant_id,
        kpi=Kpi(
            conversations_7d=int(conversations_7d),
            messages_7d=int(messages_7d),
            pending_escalations=int(pending),
            cost_30d=float(cost_30d),
            active_plugins=count_tenant_packs(await load_active_spec()),
        ),
        bot_paused=app_settings.bot_paused if app_settings else False,
        llm_label=f"{llm_provider} · {llm_model}",
        recent=[
            RecentConversation(
                id=conv.id,
                customer_name=cust.name,
                external_id=cust.external_id,
                status=conv.status,
            )
            for conv, cust in recent_rows
        ],
        billing=BillingSummary(**billing) if billing else None,
    )
"""Escalaciones a humano (lectura): `GET /api/v1/admin/escalations`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from core.api.deps import Principal, require
from core.api.pagination import Page, PageParams, build_page, page_params
from core.db.models import Conversation, Customer, Escalation
from core.db.session import get_session

router = APIRouter(prefix="/admin/escalations", tags=["admin · escalaciones"])


class EscalationItem(BaseModel):
    conversation_id: uuid.UUID
    customer_name: str | None
    external_id: str | None
    reason: str
    summary: str
    status: str
    created_at: datetime


@router.get("", response_model=Page[EscalationItem])
async def list_escalations(
    principal: Annotated[Principal, Depends(require("escalations"))],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[EscalationItem]:
    tenant_id = principal.tenant_id
    query = (
        select(Escalation, Conversation, Customer)
        .join(Conversation, Conversation.id == Escalation.conversation_id)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Escalation.tenant_id == tenant_id)
    )
    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0
        rows = (
            await session.execute(
                query.order_by(Escalation.created_at.desc())
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).all()

    items = [
        EscalationItem(
            conversation_id=conv.id,
            customer_name=cust.name,
            external_id=cust.external_id,
            reason=esc.reason,
            summary=esc.summary,
            status=esc.status,
            created_at=esc.created_at,
        )
        for esc, conv, cust in rows
    ]
    return build_page(items, int(total), params)

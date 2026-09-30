"""Leads (lectura): `GET /api/v1/admin/leads`.

Un lead es un `Customer` (ADR-0022); cada pack habilitado aporta columnas vía
`LeadProvider` (ADR-0056). El core no importa packs: se combinan los providers
registrados. Mismo patrón que la vista SSR `/admin/leads`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from core.admin.lead_registry import lead_registry
from core.agents.spec import enabled_packs, load_active_spec
from core.api.deps import Principal, require
from core.api.pagination import PageParams, page_params
from core.db.models import Customer
from core.db.session import get_session
from core.shared.test_mode import TEST_CHANNEL

router = APIRouter(prefix="/admin/leads", tags=["admin · leads"])


class LeadColumn(BaseModel):
    key: str
    label: str


class LeadRow(BaseModel):
    id: uuid.UUID
    cliente: str
    canal: str
    alta: str
    extras: dict[str, Any]


class LeadsResponse(BaseModel):
    columns: list[LeadColumn]
    items: list[LeadRow]
    total: int
    page: int
    page_size: int
    has_more: bool


@router.get("", response_model=LeadsResponse)
async def list_leads(
    principal: Annotated[Principal, Depends(require("leads"))],
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(description="Busca por nombre o identificador.")] = None,
) -> LeadsResponse:
    tenant_id = principal.tenant_id
    query = select(Customer).where(
        Customer.tenant_id == tenant_id,
        Customer.deleted_at.is_(None),
        Customer.channel != TEST_CHANNEL,
    )
    if q:
        like = f"%{q}%"
        query = query.where(or_(Customer.name.ilike(like), Customer.external_id.ilike(like)))

    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0
        customers = (
            (
                await session.execute(
                    query.order_by(Customer.created_at.desc())
                    .offset(params.offset)
                    .limit(params.page_size)
                )
            )
            .scalars()
            .all()
        )

    try:
        enabled = enabled_packs(await load_active_spec())
    except Exception:
        enabled = set()
    providers = lead_registry.providers_for(enabled)

    pack_values: dict[uuid.UUID, dict[str, Any]] = {}
    for provider in providers:
        if provider.resolver is None:
            continue
        try:
            result = await provider.resolver([c.id for c in customers])
        except Exception:
            continue
        for cid, values in (result or {}).items():
            pack_values.setdefault(cid, {}).update(values)

    columns = [LeadColumn(key=col.key, label=col.label) for p in providers for col in p.columns]
    items = [
        LeadRow(
            id=c.id,
            cliente=c.name or c.external_id,
            canal=c.channel,
            alta=c.created_at.strftime("%Y-%m-%d") if c.created_at else "—",
            extras=pack_values.get(c.id, {}),
        )
        for c in customers
    ]
    return LeadsResponse(
        columns=columns,
        items=items,
        total=int(total),
        page=params.page,
        page_size=params.page_size,
        has_more=params.page * params.page_size < total,
    )

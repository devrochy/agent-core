"""Escalaciones a humano (lectura): `GET /api/v1/admin/escalations`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from core.api.deps import Principal, require
from core.api.pagination import Page, PageParams, build_page, page_params
from core.db.models import Conversation, Customer, Escalation
from core.db.session import get_session
from core.orchestrator import motivos_escalacion as motivos
from core.orchestrator.motivos_escalacion import MotivoCodigo

router = APIRouter(prefix="/admin/escalations", tags=["admin · escalaciones"])


class EscalationItem(BaseModel):
    conversation_id: uuid.UUID
    # HU-CRM-20: el nombre abre la ficha del cliente.
    customer_id: uuid.UUID
    customer_name: str | None
    external_id: str | None
    reason: str = Field(description="Motivo tal como se guardó (crudo)")
    reason_code: MotivoCodigo = Field(description="Código cerrado del motivo (HU-UX-17)")
    reason_label: str = Field(description="Motivo legible en español")
    summary: str
    status: str
    created_at: datetime


def _habilidad_de_tool(nombre: str) -> str | None:
    """Nombre visible del plugin que registró la tool `nombre` (None si no está cargada)."""
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager

    tool = mcp_host.get_tool(nombre)
    modulo = plugin_manager.get(tool.plugin_id) if tool and tool.plugin_id else None
    return (modulo.display_name or modulo.name) if modulo else None


@router.get("", response_model=Page[EscalationItem])
async def list_escalations(
    principal: Annotated[Principal, Depends(require("escalations"))],
    params: Annotated[PageParams, Depends(page_params)],
    reason_code: Annotated[MotivoCodigo | None, Query(description="Filtra por código de motivo")] = None,
    estado: Annotated[
        Literal["pendiente", "asignada", "resuelta"] | None,
        Query(description="Filtra por estado de la escalación"),
    ] = None,
) -> Page[EscalationItem]:
    tenant_id = principal.tenant_id
    query = (
        select(Escalation, Conversation, Customer)
        .join(Conversation, Conversation.id == Escalation.conversation_id)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Escalation.tenant_id == tenant_id)
    )
    if reason_code is not None:
        query = query.where(motivos.condicion_sql(reason_code))
    if estado is not None:
        query = query.where(Escalation.status == estado)
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
            customer_id=cust.id,
            customer_name=cust.name,
            external_id=cust.external_id,
            reason=esc.reason,
            reason_code=motivos.codigo(esc.reason, esc.summary),
            reason_label=motivos.etiqueta(esc.reason, esc.summary, _habilidad_de_tool),
            summary=esc.summary,
            status=esc.status,
            created_at=esc.created_at,
        )
        for esc, conv, cust in rows
    ]
    return build_page(items, int(total), params)

"""Providers de búsqueda del core (ADR-0096): conversaciones y clientes/leads.

Ambos leen tablas del core (`conversations`, `customers`) dentro del scope de
tenant del request (RLS aplica). El matching es `ILIKE` con comodines
escapados; se devuelve lo más reciente primero.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select

from core.db.models import Conversation, Customer
from core.db.session import get_session
from core.search import ResultadoBusqueda, escapar_ilike


async def _buscar_conversaciones(
    tenant_id: uuid.UUID, q: str, limit: int
) -> list[ResultadoBusqueda]:
    patron = f"%{escapar_ilike(q)}%"
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Conversation, Customer)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.deleted_at.is_(None),
                    Customer.deleted_at.is_(None),
                    or_(
                        Customer.name.ilike(patron, escape="\\"),
                        Customer.external_id.ilike(patron, escape="\\"),
                        Customer.contact_phone.ilike(patron, escape="\\"),
                    ),
                )
                .order_by(Conversation.started_at.desc())
                .limit(limit)
            )
        ).all()
    return [
        {
            "id": str(conv.id),
            "tipo": "conversacion",
            "titulo": customer.name or customer.external_id,
            "subtitulo": "Conversación",
            "extra": {
                "customerId": str(customer.id),
                "canal": customer.channel,
                "estado": conv.status,
            },
        }
        for conv, customer in rows
    ]


async def _buscar_clientes_core(
    tenant_id: uuid.UUID, q: str, limit: int
) -> list[ResultadoBusqueda]:
    patron = f"%{escapar_ilike(q)}%"
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Customer)
                .where(
                    Customer.tenant_id == tenant_id,
                    Customer.deleted_at.is_(None),
                    or_(
                        Customer.name.ilike(patron, escape="\\"),
                        Customer.external_id.ilike(patron, escape="\\"),
                        Customer.contact_phone.ilike(patron, escape="\\"),
                    ),
                )
                .order_by(Customer.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": str(c.id),
            "tipo": "cliente",
            "titulo": c.name or c.external_id,
            "subtitulo": c.external_id,
            "extra": {"canal": c.channel, "telefono": c.contact_phone or None},
        }
        for c in rows
    ]


async def buscar_conversaciones(
    tenant_id: uuid.UUID, q: str, limit: int
) -> list[ResultadoBusqueda]:
    return await _buscar_conversaciones(tenant_id, q, limit)


async def buscar_leads(tenant_id: uuid.UUID, q: str, limit: int) -> list[ResultadoBusqueda]:
    return await _buscar_clientes_core(tenant_id, q, limit)

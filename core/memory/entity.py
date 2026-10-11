"""Capa 2 — Entity Memory (PostgreSQL).

Perfil del cliente: nombre, email, preferencias, IDs externos (ej. crm_lead_id). Desde HU-AOS-36
la fuente son los hechos de `memory_facts` (namespace `perfil`, con procedencia) y
`customers.attributes` es su proyección, que se actualiza en la misma transacción
(`core/memory/facts.py`, ADR-0114 §3). `upsert_attributes` deja `attributes` exactamente como
antes, así los lectores (`get_profile`, pack-calendar) no cambian.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Customer
from core.db.session import get_session
from core.memory import facts
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


async def get_profile(customer_id: uuid.UUID) -> dict[str, Any]:
    async with get_session() as session:
        customer = await session.get(Customer, customer_id)
        if customer is None:
            return {}
        return {
            "name": customer.name,
            "contact_phone": customer.contact_phone,
            **customer.attributes,
        }


async def upsert_attributes(
    customer_id: uuid.UUID,
    attributes: dict[str, Any],
    *,
    source: str = "tool",
    concedidos: Collection[str] | None = None,
) -> None:
    """Fusiona atributos nuevos con los existentes (los plugins llaman esto).

    Escribe hechos `perfil` con procedencia y su proyección en una sola transacción.
    `concedidos` = `writeNamespaces` del agente v3 que escribe (None en el runtime legacy).
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        log.warning("upsert_attributes_missing_tenant_context")
        return
    async with get_session() as session:
        await upsert_attributes_en_sesion(
            session, tenant_id, customer_id, attributes, source=source, concedidos=concedidos
        )


async def upsert_attributes_en_sesion(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    attributes: dict[str, Any],
    *,
    source: str = "tool",
    concedidos: Collection[str] | None = None,
) -> None:
    """Igual que `upsert_attributes`, dentro de la transacción de quien llama (p. ej. el alta
    de un cliente desde el intake de leads, que crea el cliente y su perfil juntos)."""
    await facts.escribir_hechos(
        session,
        tenant_id=tenant_id,
        customer_id=customer_id,
        namespace=facts.PERFIL,
        valores=attributes,
        source=source,
        concedidos=concedidos,
    )


async def find_by_external_id(channel: str, external_id: str) -> Customer | None:
    async with get_session() as session:
        result = await session.execute(
            select(Customer).where(Customer.channel == channel, Customer.external_id == external_id)
        )
        return result.scalar_one_or_none()


def profile_to_prompt(profile: dict[str, Any]) -> str:
    """Serializa el perfil para el bloque dinámico del system prompt."""
    if not profile:
        return ""
    lines = [f"- {key}: {value}" for key, value in profile.items() if value]
    return "\n".join(lines)

"""Capa 2 — Entity Memory (PostgreSQL).

Atributos estructurados del cliente (customers.attributes JSONB), actualizados
por los plugins vía upsert_attributes(). Es la fuente de verdad durable:
nombre, email, preferencias, IDs externos (ej. crm_lead_id).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.models import Customer
from core.db.session import get_session


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


async def upsert_attributes(customer_id: uuid.UUID, attributes: dict[str, Any]) -> None:
    """Fusiona atributos nuevos con los existentes (los plugins llaman esto)."""
    async with get_session() as session:
        customer = await session.get(Customer, customer_id)
        if customer is None:
            return
        merged = {**customer.attributes, **attributes}
        customer.attributes = merged
        if attributes.get("name"):
            customer.name = str(attributes["name"])


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

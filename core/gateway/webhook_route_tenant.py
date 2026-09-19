"""Resolución de tenant desde un evento de webhook de Meta ya aplanado
(`core/gateway/webhook_flatten.py`). Nunca descarta un evento sin resolver:
el caller decide qué hacer con `None` (hoy: registrar en la tabla de log
con `tenant_id=None` y `process_error='unresolved_tenant'`)."""

from __future__ import annotations

import uuid

from core.gateway.webhook_flatten import FlattenedEvent
from core.shared.tenant import resolve_tenant_for_phone


async def _resolve_tenant_for_waba(waba_id: str) -> uuid.UUID | None:
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(text("select resolve_tenant_for_waba(:w)"), {"w": waba_id})
        ).scalar_one_or_none()


async def _resolve_tenant_for_page(page_id: str) -> uuid.UUID | None:
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(text("select resolve_tenant_for_page(:p)"), {"p": page_id})
        ).scalar_one_or_none()


async def _resolve_tenant_for_ig_user(ig_user_id: str) -> uuid.UUID | None:
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(text("select resolve_tenant_for_ig_user(:i)"), {"i": ig_user_id})
        ).scalar_one_or_none()


async def resolve_tenant_for_event(event: FlattenedEvent) -> uuid.UUID | None:
    """Todas las funciones invocadas son SECURITY DEFINER (ADR-0012):
    bootstrap chicken-and-egg, no dependen de RLS/tenant_scope activo."""
    if event.object_type == "whatsapp_business_account":
        tenant_id = await _resolve_tenant_for_waba(event.entry_id)
        if tenant_id is not None:
            return tenant_id
        # Retrocompatible: ningún tenant real tiene `waba_accounts` poblado
        # todavía (nadie sincroniza esa tabla desde Graph API aún) -- cae al
        # camino que sí usan hoy los tenants reales, por phone_number_id.
        if event.sub_id:
            return await resolve_tenant_for_phone(event.sub_id)
        return None

    if event.object_type == "page":
        return await _resolve_tenant_for_page(event.entry_id)

    if event.object_type == "instagram":
        return await _resolve_tenant_for_ig_user(event.entry_id)

    return None

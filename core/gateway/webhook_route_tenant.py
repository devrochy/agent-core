"""Resolución de tenant desde un evento de webhook de Meta ya aplanado
(`core/gateway/webhook_flatten.py`). Nunca descarta un evento sin resolver:
el caller decide qué hacer con `None` (hoy: registrar en la tabla de log
con `tenant_id=None` y `process_error='unresolved_tenant'`)."""

from __future__ import annotations

import uuid

from core.gateway.webhook_flatten import FlattenedEvent
from core.shared.logger import get_logger
from core.shared.tenant import resolve_tenant_for_phone

log = get_logger(__name__)


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
        # Primero el número (ADR-0095): es el activo más específico y el que
        # identifica a quién le escribieron. La WABA solo decide cuando el evento
        # no trae número (plantillas, cuenta) o el número no resuelve. Si la WABA
        # mandara sola, un negocio que registrara la WABA de otro recibiría los
        # mensajes de sus números. Las dos devuelven NULL si el activo lo
        # reclaman dos negocios: nunca se elige uno al azar.
        waba_tenant = await _resolve_tenant_for_waba(event.entry_id)
        if not event.sub_id:
            return waba_tenant
        phone_tenant = await resolve_tenant_for_phone(event.sub_id)
        if phone_tenant is None:
            return waba_tenant
        if waba_tenant is not None and waba_tenant != phone_tenant:
            # Un número y su WABA son siempre del mismo negocio: si apuntan a dos,
            # una de las filas es ajena (heredada sin verificar, o cargada por una
            # vía que no comprueba la WABA). No se elige: el evento queda
            # `unresolved_tenant` y se avisa para corregirlo a mano.
            log.warning(
                "webhook_numero_y_waba_de_negocios_distintos",
                entry_id=event.entry_id,
                phone_number_id=event.sub_id,
                phone_tenant_id=str(phone_tenant),
                waba_tenant_id=str(waba_tenant),
            )
            return None
        return phone_tenant

    if event.object_type == "page":
        return await _resolve_tenant_for_page(event.entry_id)

    if event.object_type == "instagram":
        return await _resolve_tenant_for_ig_user(event.entry_id)

    return None

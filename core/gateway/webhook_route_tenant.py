"""Resolución de tenant desde un evento de webhook de Meta ya aplanado
(`core/gateway/webhook_flatten.py`). Nunca descarta un evento sin resolver:
el caller decide qué hacer con `None` (hoy: registrar en la tabla de log
con `tenant_id=None` y `process_error='unresolved_tenant'`, o `'tenant_conflict'`
si los activos del evento apuntan a más de un negocio, ADR-0095)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

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


@dataclass(frozen=True)
class EventRoute:
    """A qué negocio va un evento.

    `conflict=True`: los activos del evento apuntan a más de un negocio (un número o una
    WABA con dos dueños, o el número de uno y la WABA de otro). El evento no va a ninguno,
    tampoco al del endpoint por slug ni al de desarrollo (ADR-0095): una de las filas es
    ajena y no se elige."""

    tenant_id: uuid.UUID | None
    conflict: bool = False


async def _has_owner(*, waba_id: str | None = None, phone_number_id: str | None = None) -> bool:
    """¿Lo reclama algún negocio? Los resolvedores dan NULL con cero dueños y con dos o
    más: esto distingue los dos casos (con `p_tenant` NULL, cualquier dueño cuenta)."""
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant

    return await asset_claimed_by_other_tenant(None, waba_id=waba_id, phone_number_id=phone_number_id)


async def _route_by_waba(event: FlattenedEvent) -> EventRoute:
    tenant_id = await _resolve_tenant_for_waba(event.entry_id)
    if tenant_id is None and await _has_owner(waba_id=event.entry_id):
        log.warning("webhook_waba_de_varios_negocios", entry_id=event.entry_id)
        return EventRoute(None, conflict=True)
    return EventRoute(tenant_id)


async def route_event(event: FlattenedEvent) -> EventRoute:
    """Todas las funciones invocadas son SECURITY DEFINER (ADR-0012):
    bootstrap chicken-and-egg, no dependen de RLS/tenant_scope activo."""
    if event.object_type == "whatsapp_business_account":
        # Primero el número (ADR-0095): es el activo más específico y el que
        # identifica a quién le escribieron. La WABA decide cuando el evento no
        # trae número (plantillas, cuenta) o el número no tiene dueño. Si la WABA
        # mandara sola, un negocio que registrara la WABA de otro recibiría los
        # mensajes de sus números.
        if not event.sub_id:
            return await _route_by_waba(event)
        phone_tenant = await resolve_tenant_for_phone(event.sub_id)
        if phone_tenant is None:
            if await _has_owner(phone_number_id=event.sub_id):
                # Número con dos dueños: no decide la WABA, porque quien la tiene
                # podría provocar la ambigüedad para quedarse con los mensajes.
                log.warning(
                    "webhook_numero_de_varios_negocios", entry_id=event.entry_id, phone_number_id=event.sub_id
                )
                return EventRoute(None, conflict=True)
            return await _route_by_waba(event)
        waba_tenant = await _resolve_tenant_for_waba(event.entry_id)
        if waba_tenant is not None and waba_tenant != phone_tenant:
            # Un número y su WABA son siempre del mismo negocio: si apuntan a dos,
            # una de las filas es ajena (heredada sin verificar, o cargada por una
            # vía que no comprueba la WABA).
            log.warning(
                "webhook_numero_y_waba_de_negocios_distintos",
                entry_id=event.entry_id,
                phone_number_id=event.sub_id,
                phone_tenant_id=str(phone_tenant),
                waba_tenant_id=str(waba_tenant),
            )
            return EventRoute(None, conflict=True)
        return EventRoute(phone_tenant)

    if event.object_type == "page":
        return EventRoute(await _resolve_tenant_for_page(event.entry_id))

    if event.object_type == "instagram":
        return EventRoute(await _resolve_tenant_for_ig_user(event.entry_id))

    return EventRoute(None)


async def resolve_tenant_for_event(event: FlattenedEvent) -> uuid.UUID | None:
    """El negocio del evento, o None si no resuelve o hay conflicto (ver `route_event`)."""
    return (await route_event(event)).tenant_id

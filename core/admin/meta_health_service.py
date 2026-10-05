"""Salud de conexiones Meta y suscripciones de webhook (fuente única SSR + API).

- `health_overview`: conexiones del tenant con su último diagnóstico.
- `diagnose_connection`: corre los checks on-demand y persiste el resultado.
- `resubscribe_waba`: reencola SOLO la suscripción de la WABA (nunca el OAuth).
- `set_billing_status`: persiste el estado que un admin marcó a mano (billing no
  tiene API en Meta).
- `webhook_subscriptions_status`: estado de la suscripción de APP por canal.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.models import MetaConnection, utcnow
from core.db.session import get_owned, get_session


async def health_overview(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    from core.gateway.meta_credentials import access_token_expires_at

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(MetaConnection).where(MetaConnection.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    out: list[dict[str, Any]] = []
    for c in rows:
        exp = await access_token_expires_at(c)
        out.append(
            {
                "id": c.id,
                "channel": c.channel,
                "environment": c.environment,
                "status": c.status,
                "status_reason": c.status_reason,
                "last_health_check_at": c.last_health_check_at,
                "health": dict(c.health or {}),
                "business_id": c.business_id,
                "meta_app_id": c.meta_app_id,
                "token_expires_at": exp,
            }
        )
    return out


async def diagnose_connection(tenant_id: uuid.UUID, connection_id: str) -> str | None:
    """Corre los checks y persiste estado. Devuelve un error legible o None."""
    from core.gateway.meta_health import decide_status, run_health_checks

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, cid, tenant_id)
    if connection is None:
        return "Conexión no encontrada"

    checks = await run_health_checks(connection, tenant_id)
    new_status, reason = decide_status(checks)

    async with get_session() as session:
        db_connection = await get_owned(session, MetaConnection, cid, tenant_id)
        if db_connection is not None:
            db_connection.health = checks
            db_connection.status = new_status
            db_connection.status_reason = reason
            db_connection.last_health_check_at = utcnow()
    return None


async def resubscribe_waba(tenant_id: uuid.UUID, connection_id: str) -> str | None:
    """Reparación inline del check `webhook_subscribed` (solo la WABA)."""
    from core.admin.meta_numbers_service import resolve_waba_and_credentials
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import _subscribe_waba_to_app

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None or waba is None or creds is None:
        return "No hay conexión/WABA lista todavía"

    try:
        await _subscribe_waba_to_app(
            creds.access_token,
            waba.waba_id,
            creds.graph_api_version,
            connection.id,
            creds.app_secret,
            tenant_id,
        )
    except GraphError as exc:
        return exc.message[:200]
    except Exception as exc:
        return str(exc)[:200]
    return None


async def set_billing_status(
    tenant_id: uuid.UUID, connection_id: str, manual_status: str
) -> str | None:
    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, cid, tenant_id)
        if connection is None:
            return "Conexión no encontrada"
        health = dict(connection.health or {})
        health["billing"] = {"manual_status": manual_status}
        connection.health = health
    return None


async def webhook_subscriptions_status(tid: uuid.UUID | None) -> list[dict[str, Any]]:
    """Una fila por canal conectado, con el estado de su suscripción de APP."""
    from core.gateway.meta_subscriptions import OBJECT_TO_CHANNEL, list_app_subscriptions

    if tid is None:
        return []
    channel_to_object = {v: k for k, v in OBJECT_TO_CHANNEL.items()}

    async with get_session() as session:
        connections = (
            (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tid, MetaConnection.status == "connected"
                    )
                )
            )
            .scalars()
            .all()
        )

    rows: list[dict[str, Any]] = []
    for connection in connections:
        object_ = channel_to_object.get(connection.channel)
        if object_ is None:
            continue
        subs = await list_app_subscriptions(connection.meta_app_id)
        sub = next((s for s in subs if s.object == object_), None)
        rows.append(
            {
                "channel": connection.channel,
                "object": object_,
                "meta_app_id": connection.meta_app_id,
                "callback_url": sub.callback_url if sub else None,
                "active": sub.active if sub else None,
                "fields": sub.fields if sub else [],
                "last_error": sub.last_error if sub else None,
            }
        )
    return rows


async def register_channel_subscription(tid: uuid.UUID, channel: str) -> str | None:
    """Registra la suscripción de APP para un canal (owner/master). Error o None."""
    from core.config import get_settings
    from core.gateway.meta_preflight import resolve_app_credentials
    from core.gateway.meta_subscriptions import (
        OBJECT_TO_CHANNEL,
        SubscriptionError,
        humanize_subscription_error,
        upsert_app_subscription,
    )
    from core.shared.exceptions import AgentCoreError
    from core.shared.tenant import tenant_scope

    channel_to_object = {v: k for k, v in OBJECT_TO_CHANNEL.items()}
    object_ = channel_to_object.get(channel)
    if object_ is None:
        return f"Canal desconocido: {channel}"

    config = await resolve_app_credentials(tid, channel)
    if config is None:
        return "No hay app de Meta configurada para este canal"

    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    try:
        async with tenant_scope(tid):
            await upsert_app_subscription(
                config, object_, callback_url=callback_url, verify_token=settings.whatsapp_verify_token
            )
    except SubscriptionError as exc:
        return str(exc)[:140]
    except AgentCoreError as exc:  # GraphError
        return humanize_subscription_error(exc)[:140]
    return None


async def set_connection_status(
    tenant_id: uuid.UUID, connection_id: str, *, active: bool
) -> str | None:
    """Activa (`connected`) o desactiva (`revoked`) una conexión Meta del tenant."""

    from core.db.models import MetaConnection as _Meta

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    async with get_session() as session:
        connection = await get_owned(session, _Meta, cid, tenant_id)
        if connection is None:
            return "Conexión no encontrada"
        if active and connection.status == "draft":
            # Un alta de Embedded Signup que nunca canjeó su `code`: no tiene token y
            # sus filas, si quedaron de antes, no están verificadas (ADR-0095).
            return "La conexión no terminó el alta: complétala con el asistente de WhatsApp"
        if active:
            connection.status = "connected"
            connection.status_reason = None
        else:
            connection.status = "revoked"
            connection.status_reason = "Desactivada a mano desde el panel"
    return None


async def soft_delete_connection(tenant_id: uuid.UUID, connection_id: str) -> str | None:
    """Soft-delete (invariante 8) liberando números y WABAs de la conexión."""
    from core.db.models import MetaConnection as _Meta
    from core.db.models import PhoneNumber as _Phone
    from core.db.models import WabaAccount as _Waba

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    async with get_session() as session:
        connection = await get_owned(session, _Meta, cid, tenant_id)
        if connection is None:
            return "Conexión no encontrada"
        connection.deleted_at = utcnow()
        connection.status = "revoked"
        await session.execute(
            _Phone.__table__.delete().where(
                _Phone.connection_id == connection.id, _Phone.tenant_id == tenant_id
            )
        )
        await session.execute(
            _Waba.__table__.delete().where(
                _Waba.connection_id == connection.id, _Waba.tenant_id == tenant_id
            )
        )
    return None

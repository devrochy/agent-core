"""Conexión y sesión del canal `waweb` (ADR-0074).

Crea/obtiene la `ChannelConnection` (provider `waweb`) y la `WaWebSession`
asociada, y resuelve el estado que muestra el panel: si el admin ya aceptó el
aviso de riesgo, el QR vigente (publicado por el connector en Redis) y la
política anti-baneo del tenant.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.models import ChannelConnection, WaWebSession, utcnow
from core.db.session import get_session
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.waweb import handoff
from core.waweb.policy import RISK_ACK_VERSION, WaWebPolicy, load_policy

log = get_logger(__name__)

WAWEB_PROVIDER = "waweb"
WAWEB_TYPE_ID = "whatsapp_waweb"
WAWEB_CATEGORY = "comunicacion"
WAWEB_LABEL = "WhatsApp (vínculo por QR)"


async def get_active_connection(tenant_id: uuid.UUID) -> ChannelConnection | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(ChannelConnection).where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.provider == WAWEB_PROVIDER,
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def get_or_create_connection(tenant_id: uuid.UUID) -> ChannelConnection:
    """Conexión `waweb` del tenant (una sola viva). La crea si no existe.

    `credentials_encrypted` guarda `{connection_id}` (no hay secreto de red
    todavía): es lo que permite que `resolve_credentials` devuelva el provider
    `waweb` y el adaptador sepa qué sesión usar.
    """
    existing = await get_active_connection(tenant_id)
    if existing is not None:
        return existing
    async with get_session() as session:
        connection = ChannelConnection(
            tenant_id=tenant_id,
            channel="whatsapp",
            provider=WAWEB_PROVIDER,
            type_id=WAWEB_TYPE_ID,
            category=WAWEB_CATEGORY,
            label=WAWEB_LABEL,
            # `external_id` estable y único por tenant: es la clave que el
            # consumer usa para resolver la conexión del inbound
            # (`ChannelConnection.external_id == connection_external_id`).
            external_id=f"waweb:{tenant_id}",
            active=True,
            is_primary=False,
        )
        session.add(connection)
        await session.flush()
        connection.credentials_encrypted = get_secret_box().encrypt_json(
            {"connection_id": str(connection.id)}
        )
        return connection


async def get_or_create_session(
    tenant_id: uuid.UUID, connection: ChannelConnection | None = None
) -> tuple[ChannelConnection, WaWebSession]:
    conn = connection or await get_or_create_connection(tenant_id)
    async with get_session() as session:
        existing = (
            await session.execute(
                select(WaWebSession).where(
                    WaWebSession.tenant_id == tenant_id,
                    WaWebSession.connection_id == conn.id,
                    WaWebSession.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return conn, existing
        wa_session = WaWebSession(
            tenant_id=tenant_id, connection_id=conn.id, status="pending_qr"
        )
        session.add(wa_session)
        await session.flush()
        return conn, wa_session


async def accept_risk(tenant_id: uuid.UUID, admin_id: uuid.UUID | None) -> WaWebSession:
    """Registra la aceptación del aviso de riesgo antes de mostrar el QR."""
    _conn, wa_session = await get_or_create_session(tenant_id)
    async with get_session() as session:
        row = await session.get(WaWebSession, wa_session.id)
        if row is None or row.tenant_id != tenant_id:
            raise ValueError("Sesión waweb no encontrada")
        row.risk_ack_at = utcnow()
        row.risk_ack_by = admin_id
        row.risk_ack_version = RISK_ACK_VERSION
        if not row.status or row.status == "error":
            row.status = "pending_qr"
        await session.flush()
        return row


async def reset_session(tenant_id: uuid.UUID) -> WaWebSession:
    """Borra la sesión vinculada y vuelve a `pending_qr` (para re-vincular)."""
    _conn, wa_session = await get_or_create_session(tenant_id)
    await handoff.clear_qr(str(tenant_id))
    async with get_session() as session:
        row = await session.get(WaWebSession, wa_session.id)
        if row is None or row.tenant_id != tenant_id:
            raise ValueError("Sesión waweb no encontrada")
        row.status = "pending_qr"
        row.jid = None
        row.creds_encrypted = None
        row.last_error = None
        await session.flush()
        return row


def _risk_ok(row: WaWebSession) -> bool:
    return row.risk_ack_at is not None and row.risk_ack_version == RISK_ACK_VERSION


async def session_view(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Estado completo para la vista del panel."""
    conn, wa_session = await get_or_create_session(tenant_id)
    risk_ok = _risk_ok(wa_session)
    qr = await handoff.read_qr(str(tenant_id)) if risk_ok else None
    policy: WaWebPolicy = await load_policy(tenant_id)
    return {
        "connection_id": conn.id,
        "status": wa_session.status,
        "jid": wa_session.jid,
        "last_error": wa_session.last_error,
        "risk_ok": risk_ok,
        "risk_ack_at": wa_session.risk_ack_at,
        "qr": qr,
        "policy": policy,
        "risk_version": RISK_ACK_VERSION,
    }

"""Costo por mensaje de WhatsApp vía Twilio (ADR-0186 §11, PR-5).

Cuando un mensaje llega a su estado final (`delivered`, `read`, `failed`, `undelivered`), el webhook de estados
encola `core.twilio.costo_mensaje`. La tarea lee el recurso Message (`price`, `price_unit`) y guarda UNA fila en
`message_costs` con `provider='twilio'` (único parcial de 0122: repetirla no duplica). Twilio puede tardar en
calcular el precio: sin precio todavía, la tarea se reintenta más tarde.

Lo que la documentación de Twilio no aclara: si `price` incluye el cargo de Meta además de la tarifa de Twilio.
La ficha lo dice así; el cargo de Meta, si no viene aquí, sale de la facturación de Twilio.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Literal

import httpx
from agentcore_channel_twilio.messages import get_message_price
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import ChannelConnection, Conversation, Message, MessageCost, utcnow
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger

log = get_logger(__name__)

ESTADOS_FINALES = frozenset({"delivered", "read", "failed", "undelivered"})
Resultado = Literal[
    "guardado",
    "ya_estaba",
    "sin_precio",
    "sin_conexion",
    "sin_mensaje",
    "error_transitorio",
    "error",
]


async def registrar_costo(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    message_sid: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Resultado:
    """Guarda el costo del mensaje si Twilio ya lo tiene. Dentro del `tenant_scope` del negocio."""
    from agentcore_channel_twilio.client import TwilioClient

    async with get_session() as session:
        ya = (
            await session.execute(
                select(MessageCost.id).where(
                    MessageCost.tenant_id == tenant_id,
                    MessageCost.provider == "twilio",
                    MessageCost.external_message_id == message_sid,
                )
            )
        ).first()
        if ya is not None:
            return "ya_estaba"
        # Como Meta (ADR-0100): solo se guarda el costo de un mensaje de ESTE negocio. Un SID ajeno (otra
        # integración de la misma cuenta, el mensaje de prueba, un estado tardío) no deja fila.
        message_id = (
            await session.execute(
                select(Message.id).where(
                    Message.tenant_id == tenant_id, Message.external_message_id == message_sid
                )
            )
        ).scalar_one_or_none()
        if message_id is None:
            return "sin_mensaje"
        connection = (
            await session.execute(
                select(ChannelConnection).where(
                    ChannelConnection.id == connection_id,
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.provider == "twilio",
                )
            )
        ).scalar_one_or_none()
    # Una conexión borrada después del envío igual tiene su costo: se lee con sus credenciales guardadas.
    if connection is None or not connection.credentials_encrypted:
        return "sin_conexion"
    creds = get_secret_box().decrypt_json(connection.credentials_encrypted)
    try:
        client = TwilioClient(
            str(creds.get("account_sid") or ""),
            str(creds.get("auth_token") or ""),
            transport=transport,
        )
        precio = await get_message_price(client, message_sid)
    except ProviderError as exc:
        transitorio = exc.category in ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA")
        log.warning(
            "twilio_costo_error", tenant_id=str(tenant_id), code=exc.code, category=exc.category
        )
        return "error_transitorio" if transitorio else "error"
    if precio.price is None:
        return "sin_precio"

    async with get_session() as session:
        resultado = await session.execute(
            pg_insert(MessageCost)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                message_id=message_id,
                external_message_id=message_sid,
                provider="twilio",
                price=precio.price,
                price_unit=precio.price_unit or None,
                status=precio.status or None,
                # `billable` es la semántica de Meta (la discrepancia de volumen la lee): Twilio no la informa.
                billable=None,
                event_ts=utcnow(),
                raw={
                    "price": str(precio.price),
                    "price_unit": precio.price_unit,
                    "status": precio.status,
                },
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "external_message_id"],
                index_where=MessageCost.provider == "twilio",
            )
        )
    guardado = bool(resultado.rowcount)
    log.info(
        "twilio_costo_guardado" if guardado else "twilio_costo_repetido", tenant_id=str(tenant_id)
    )
    return "guardado" if guardado else "ya_estaba"


async def resumen(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, *, dias: int = 30
) -> dict[str, object]:
    """Costos de Twilio de los mensajes de las conversaciones de esta conexión en los últimos `dias`, por moneda."""
    desde = utcnow() - timedelta(days=dias)
    async with get_session() as session:
        filas = (
            await session.execute(
                select(
                    MessageCost.price_unit,
                    func.count(MessageCost.id),
                    func.coalesce(func.sum(MessageCost.price), 0),
                )
                .join(
                    Message,
                    (Message.id == MessageCost.message_id)
                    & (Message.tenant_id == MessageCost.tenant_id),
                )
                .join(
                    Conversation,
                    (Conversation.id == Message.conversation_id)
                    & (Conversation.tenant_id == Message.tenant_id),
                )
                .where(
                    MessageCost.tenant_id == tenant_id,
                    MessageCost.provider == "twilio",
                    MessageCost.event_ts >= desde,
                    Conversation.connection_id == connection_id,
                )
                .group_by(MessageCost.price_unit)
            )
        ).all()
    return {
        "dias": dias,
        "por_moneda": [
            {"moneda": (u or "").upper(), "mensajes": int(n), "total": str(t)} for u, n, t in filas
        ],
    }

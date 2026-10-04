"""Encolado de correo saliente reutilizable (ADR-0071, HU-CAL-02).

El canal de correo (SMTP) modela un correo como un `Message` del canal `email`
y lo envía por la cola de `outbound_send`. La tool `send_email` y consumidores
del servidor (p. ej. la confirmación de una cita) comparten esta función para no
duplicar la resolución de `Customer`/`Conversation`.

Devuelve el `message_id` (str) o lanza `EmailOutboxError` con un motivo estable
para que el llamador decida (registrar el fallo y seguir, no romper el flujo).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.db.models import Conversation, Customer, Message
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

_SENTINEL = uuid.UUID("00000000-0000-0000-0000-000000000000")


class EmailOutboxError(RuntimeError):
    """No se pudo encolar el correo (sin conexión, destinatario inválido, etc.)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def queue_outbound_email(to: str, subject: str, body: str) -> str:
    """Encola un correo saliente para `to` en el tenant actual.

    `reason` posibles (EmailOutboxError): `no_tenant`, `invalid_recipient`,
    `no_connection`.
    """
    from core.gateway.channels.email import single_recipient

    tenant_id = get_current_tenant_id()
    if tenant_id is None or tenant_id == _SENTINEL:
        raise EmailOutboxError("no_tenant")

    recipient = single_recipient(to)
    if recipient is None:
        raise EmailOutboxError("invalid_recipient")
    to = recipient

    body = (body or "").strip()
    if not body:
        raise EmailOutboxError("empty_body")
    subject = (subject or "").strip().replace("\r", " ").replace("\n", " ")

    from core.gateway.connections import get_active_connection

    connection = await get_active_connection("email_smtp")
    if connection is None:
        raise EmailOutboxError("no_connection")

    async with get_session() as session:
        customer = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.channel == "email",
                    Customer.external_id == to,
                    Customer.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if customer is None:
            customer = Customer(tenant_id=tenant_id, channel="email", external_id=to)
            session.add(customer)
            await session.flush()

        conversation = (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.customer_id == customer.id,
                    Conversation.connection_id == connection.id,
                    Conversation.status == "open",
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if conversation is None:
            conversation = Conversation(
                tenant_id=tenant_id,
                customer_id=customer.id,
                channel="email",
                connection_id=connection.id,
                status="open",
            )
            session.add(conversation)
            await session.flush()

        content = f"Subject: {subject or '(sin asunto)'}\n\n{body}"
        message = Message(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            direction="outbound",
            sender_type="agent",
            content=content,
            status="queued",
            client_message_id=str(uuid.uuid4()),
        )
        session.add(message)
        await session.flush()
        message_id = message.id

    from core.tasks.outbound_send import outbound_send_task

    outbound_send_task.delay(str(message_id), str(tenant_id))
    return str(message_id)

"""Tool `send_email`: crea cliente/conversación de correo y encola el envío (ADR-0071)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _cleanup(tenant_id: uuid.UUID) -> None:
    from sqlalchemy import delete

    from core.db.models import ChannelConnection, Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(delete(Message).where(Message.tenant_id == tenant_id))
            await session.execute(delete(Conversation).where(Conversation.tenant_id == tenant_id))
            await session.execute(delete(Customer).where(Customer.tenant_id == tenant_id))
            await session.execute(
                delete(ChannelConnection).where(ChannelConnection.tenant_id == tenant_id)
            )


async def test_send_email_crea_mensaje_cliente_y_conversacion(monkeypatch):
    import core.tasks.outbound_send as outbound
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.gateway.connections import save_connection
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope
    from core.tools.core_skill import CoreSkill

    tenant_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    to = f"dest-{suffix}@ejemplo.com"
    queued: list[tuple] = []
    monkeypatch.setattr(outbound.outbound_send_task, "delay", lambda *a, **k: queued.append(a))

    try:
        async with tenant_scope(tenant_id):
            await save_connection(
                "correo",
                {
                    "host": "smtp.ejemplo.com",
                    "port": 587,
                    "username": "u",
                    "password": "p",
                    "from_address": f"from-{suffix}@ejemplo.com",
                    "use_tls": True,
                },
                type_id="email_smtp",
            )
            ctx = ToolContext(
                conversation_id=uuid.uuid4(),
                customer_id=uuid.uuid4(),
                idempotency_key="test",
                channel="whatsapp",
            )
            result = await CoreSkill()._send_email(
                {"to": to, "subject": "Confirmación", "body": "Tu cita quedó agendada."}, ctx
            )

            assert result["status"] == "queued"
            assert len(queued) == 1, "el envío se encoló una vez"

            async with get_session() as session:
                customer = (
                    await session.execute(
                        select(Customer).where(
                            Customer.tenant_id == tenant_id, Customer.external_id == to
                        )
                    )
                ).scalar_one()
                assert customer.channel == "email"

                conversation = (
                    await session.execute(
                        select(Conversation).where(
                            Conversation.tenant_id == tenant_id,
                            Conversation.customer_id == customer.id,
                        )
                    )
                ).scalar_one()
                assert conversation.channel == "email"

                message = (
                    await session.execute(
                        select(Message).where(Message.conversation_id == conversation.id)
                    )
                ).scalars().first()
                assert message is not None
                assert message.status == "queued"
                assert message.content.startswith("Subject: Confirmación")
    finally:
        await _cleanup(tenant_id)

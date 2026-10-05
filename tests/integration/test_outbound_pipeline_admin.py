"""Flujo completo de la cola de salida contra la BD real: encolar crea la
fila `queued` con `client_message_id` único (constraint real, no solo el
no-op de la tarea); la tarea de envío persiste el wamid y pasa a `sent`;
el webhook de `statuses` marca `delivered` y un `delivered` tardío después
de `read` no degrada."""

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


async def _setup_conversation(moto_setup, suffix: str):
    from core.db.models import ChannelConnection, Conversation, Customer
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    t1, _moto = moto_setup

    async with tenant_scope(t1):
        async with get_session() as session:
            # `is_primary=False` (default) a propósito: la conversación ya
            # referencia esta conexión por `connection_id` explícito, así
            # que `resolve_credentials` la resuelve por ese camino y nunca
            # llega al fallback de "conexión primaria" -- marcarla primaria
            # acá ensuciaría ese fallback para CUALQUIER otro test que
            # comparta el tenant fijo `TEST_ALPHA_ID` (moto_setup).
            channel_connection = ChannelConnection(
                tenant_id=t1, channel="whatsapp", provider="meta", external_id=f"phone-{suffix}",
                active=True,
                credentials_encrypted=get_secret_box().encrypt_json(
                    {"phone_number_id": f"phone-{suffix}", "access_token": "tok-de-prueba"}
                ),
            )
            session.add(channel_connection)
            await session.flush()

            customer = Customer(tenant_id=t1, channel="whatsapp", external_id=f"wa:outq-{suffix}")
            session.add(customer)
            await session.flush()

            conversation = Conversation(
                tenant_id=t1, customer_id=customer.id, connection_id=channel_connection.id,
            )
            session.add(conversation)
            await session.flush()
            return t1, conversation.id


async def test_encolar_envio_wamid_y_statuses_sin_degradar(moto_setup, monkeypatch):
    import core.tasks.outbound_send as outbound_send_mod
    from core.db.models import Message
    from core.db.session import get_session
    from core.gateway.meta_message_status import apply_status_update
    from core.gateway.sender import send_to_conversation
    from core.shared.tenant import tenant_scope
    from core.tasks.outbound_send import _send_one

    suffix = uuid.uuid4().hex[:8]
    t1, conversation_id = await _setup_conversation(moto_setup, suffix)

    wamid = f"wamid.TEST.{suffix}"

    class _FakeAdapter:
        async def send_text(self, credentials, to_external_id, text):
            return wamid

        async def send_image(self, credentials, to_external_id, image_url, caption=""):
            return wamid

    # Pisa el stub global de `_stub_llm_and_outbound` (conftest) -- acá
    # necesitamos un wamid propio, no el `None` genérico del FakeOutbound
    # compartido por el resto de la suite de integración.
    monkeypatch.setattr(outbound_send_mod, "outbound_adapter_for", lambda *a, **k: _FakeAdapter())

    async with tenant_scope(t1):
        # Encolar -- una sola burbuja para simplificar el assert de wamid único.
        ok = await send_to_conversation(conversation_id, "hola desde la cola")
        assert ok is True

        async with get_session() as session:
            queued = (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id, Message.direction == "outbound")
                )
            ).scalars().first()
        assert queued is not None
        assert queued.status == "queued"
        assert queued.client_message_id is not None

        # Se llama a `_send_one` (la lógica async pura) directo, no al
        # wrapper sync de Celery (`outbound_send_task`, que envuelve todo
        # en `asyncio.run()` -- chocaría con el loop ya corriendo del test).
        error = await _send_one(queued.id, t1)
        assert error is None

        async with get_session() as session:
            sent = await session.get(Message, queued.id)
        assert sent.status == "sent"
        assert sent.external_message_id == wamid

        # Webhook de estado: delivered -> read -> un delivered tardío no degrada.
        await apply_status_update({"id": wamid, "status": "delivered", "timestamp": "1900000000"}, expected_tenant_id=t1)
        await apply_status_update({"id": wamid, "status": "read", "timestamp": "1900000100"}, expected_tenant_id=t1)
        await apply_status_update({"id": wamid, "status": "delivered", "timestamp": "1900000050"}, expected_tenant_id=t1)

        async with get_session() as session:
            final = await session.get(Message, sent.id)
        assert final.status == "read"


async def test_client_message_id_unico_por_tenant_a_nivel_de_bd(moto_setup):
    """El índice único parcial es el segundo cinturón real: dos filas con
    el mismo client_message_id para el mismo tenant no pueden coexistir."""
    from sqlalchemy.exc import IntegrityError

    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    suffix = uuid.uuid4().hex[:8]
    t1, conversation_id = await _setup_conversation(moto_setup, suffix)
    client_message_id = str(uuid.uuid4())

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=t1, conversation_id=conversation_id, direction="outbound", sender_type="agent",
                    content="uno", status="queued", client_message_id=client_message_id,
                )
            )

        with pytest.raises(IntegrityError):
            async with get_session() as session:
                session.add(
                    Message(
                        tenant_id=t1, conversation_id=conversation_id, direction="outbound", sender_type="agent",
                        content="dos", status="queued", client_message_id=client_message_id,
                    )
                )

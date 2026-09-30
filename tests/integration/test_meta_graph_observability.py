"""Cliente único de Graph API contra el stack real: graph_fetch/record_usage
persisten con RLS (tenant_id correcto, aislado), y el freno de envío al 90%
de uso realmente frena `send_to_conversation` (no solo queda como flag suelto)."""

from __future__ import annotations

import json
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


async def _make_connection(tenant_id, suffix: str):
    from core.db.models import MetaConnection
    from core.db.session import get_session

    async with get_session() as session:
        connection = MetaConnection(
            tenant_id=tenant_id, channel="whatsapp", environment="production", meta_app_id=f"app-{suffix}"
        )
        session.add(connection)
        await session.flush()
        return connection.id


async def test_graph_fetch_registra_call_y_usage_bajo_rls(monkeypatch, moto_setup):
    import core.gateway.meta_graph as graph_mod
    from core.db.models import GraphApiCall, GraphApiUsage
    from core.db.session import get_session
    from core.gateway.meta_graph import graph_fetch
    from core.shared.tenant import tenant_scope

    t1, moto = moto_setup
    suffix = uuid.uuid4().hex[:8]

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {"x-app-usage": json.dumps({"call_count": 10})}
            self.text = ""

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", FakeAsyncClient())

    async with tenant_scope(t1):
        conn_id = await _make_connection(t1, suffix)
        await graph_fetch(f"/app-{suffix}", token="tok", app_secret="secret", connection_id=conn_id)

        async with get_session() as session:
            call = (
                await session.execute(select(GraphApiCall).where(GraphApiCall.connection_id == conn_id))
            ).scalar_one()
            assert call.endpoint == f"/app-{suffix}"
            assert call.status_code == 200

            usage = (
                await session.execute(select(GraphApiUsage).where(GraphApiUsage.connection_id == conn_id))
            ).scalar_one()
            assert usage.call_count == 1

    async with tenant_scope(moto):
        async with get_session() as session:
            leak = (
                await session.execute(select(GraphApiCall).where(GraphApiCall.connection_id == conn_id))
            ).scalar_one_or_none()
            assert leak is None, "la llamada de t1 no debe verse desde moto"


async def test_freno_al_90_por_ciento_bloquea_send_to_conversation(monkeypatch, moto_setup):
    from core.db.models import (
        ChannelConnection,
        Conversation,
        Customer,
        MetaConnection,
        PhoneNumber,
    )
    from core.db.session import get_session
    from core.gateway import sender
    from core.gateway.meta_usage import set_pause_flag
    from core.shared.tenant import tenant_scope

    t1, _moto = moto_setup
    suffix = uuid.uuid4().hex[:8]

    async with tenant_scope(t1):
        async with get_session() as session:
            meta_connection = MetaConnection(
                tenant_id=t1, channel="whatsapp", environment="production", meta_app_id=f"app-{suffix}"
            )
            session.add(meta_connection)
            await session.flush()

            session.add(
                PhoneNumber(
                    tenant_id=t1, connection_id=meta_connection.id, phone_number_id=f"phone-{suffix}"
                )
            )

            from core.shared.crypto import get_secret_box

            channel_connection = ChannelConnection(
                tenant_id=t1,
                channel="whatsapp",
                provider="meta",
                external_id=f"phone-{suffix}",
                active=True,
                is_primary=True,
                credentials_encrypted=get_secret_box().encrypt_json(
                    {"phone_number_id": f"phone-{suffix}", "access_token": "tok-de-prueba"}
                ),
            )
            session.add(channel_connection)
            await session.flush()

            customer = Customer(tenant_id=t1, channel="whatsapp", external_id=f"wa:pause-{suffix}")
            session.add(customer)
            await session.flush()

            conversation = Conversation(
                tenant_id=t1, customer_id=customer.id, connection_id=channel_connection.id
            )
            session.add(conversation)
            await session.flush()
            conversation_id = conversation.id

        await set_pause_flag(t1, meta_connection.id)

        delivered = await sender.send_to_conversation(conversation_id, "hola, esto no debería salir")
        assert delivered is False

        async with get_session() as session:
            from core.db.models import Message

            last_message = (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id, Message.direction == "outbound")
                    .order_by(Message.created_at.desc())
                )
            ).scalars().first()
            assert last_message is not None
            assert last_message.status == "failed"

"""`send_messenger_message` solo chequea `thread_owner` si Conversation
Routing está activo para esa Página -- evita una llamada de Graph API
extra en el caso común. Si está activo y no somos dueños del hilo, bloquea
el envío ANTES de llamar a `/messages`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.gateway.meta_messenger as messenger_mod
from core.gateway.meta_messenger import MessengerSendError, send_messenger_message


@pytest.mark.asyncio
async def test_routing_desactivado_no_llama_a_thread_owner(monkeypatch):
    calls = []

    async def _fake_graph_fetch(path, **kwargs):
        calls.append(path)
        if path.endswith("/thread_owner"):
            raise AssertionError("no debe chequear thread_owner si routing está desactivado")
        return {"message_id": "mid.1"}

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    expires = datetime.now(UTC) + timedelta(hours=1)
    await send_messenger_message(
        "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(),
        service_window_expires_at=expires, conversation_routing_enabled=False,
    )
    assert not any(p.endswith("/thread_owner") for p in calls)


@pytest.mark.asyncio
async def test_routing_activo_y_no_somos_dueños_bloquea_sin_llamar_a_messages(monkeypatch):
    monkeypatch.setattr(messenger_mod, "get_settings", lambda: SimpleNamespace(meta_app_id="nuestra-app", meta_app_secret="s"))

    async def _fake_graph_fetch(path, **kwargs):
        if path.endswith("/thread_owner"):
            return {"data": [{"thread_owner": {"app_id": "otra-app"}}]}
        raise AssertionError("no debe llamar a /messages si no somos dueños del hilo")

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    expires = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(MessengerSendError, match="control"):
        await send_messenger_message(
            "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(),
            service_window_expires_at=expires, conversation_routing_enabled=True,
        )


@pytest.mark.asyncio
async def test_routing_activo_y_somos_dueños_envia_normal(monkeypatch):
    monkeypatch.setattr(messenger_mod, "get_settings", lambda: SimpleNamespace(meta_app_id="nuestra-app", meta_app_secret="s"))

    async def _fake_graph_fetch(path, **kwargs):
        if path.endswith("/thread_owner"):
            return {"data": [{"thread_owner": {"app_id": "nuestra-app"}}]}
        return {"message_id": "mid.2"}

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    expires = datetime.now(UTC) + timedelta(hours=1)
    message_id = await send_messenger_message(
        "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(),
        service_window_expires_at=expires, conversation_routing_enabled=True,
    )
    assert message_id == "mid.2"


@pytest.mark.asyncio
async def test_extend_thread_control_rechaza_mas_de_7_dias(monkeypatch):
    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe llamar a Meta si el límite local ya rechaza")

    monkeypatch.setattr(messenger_mod, "graph_fetch", _debe_no_llamarse)

    with pytest.raises(messenger_mod.MessengerError, match="7"):
        await messenger_mod.extend_thread_control("page-1", "psid-1", "tok", "v21.0", uuid.uuid4(), days=10)

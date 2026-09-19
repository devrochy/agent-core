"""Message tags de 2026: CONFIRMED_EVENT_UPDATE/ACCOUNT_UPDATE/
POST_PURCHASE_UPDATE fueron retirados el 10/feb/2026 -- no deben existir en
ningún literal del módulo. Un envío fuera de ventana sin confirmación
humana falla en validación LOCAL, nunca llama a Meta."""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import core.gateway.meta_messenger as messenger_mod
from core.gateway.meta_messenger import (
    ALLOWED_MESSAGE_TAGS,
    MessengerSendError,
    send_messenger_message,
)

_RETIRED_TAGS = {"CONFIRMED_EVENT_UPDATE", "ACCOUNT_UPDATE", "POST_PURCHASE_UPDATE"}


def test_tags_retirados_no_existen_como_literal_de_codigo():
    """Se permite MENCIONARLOS en comentarios/docstrings explicando por qué
    están excluidos -- lo que no debe existir es el tag como STRING LITERAL
    usable (ej. en un set/dict/comparación)."""
    source = inspect.getsource(messenger_mod)
    for tag in _RETIRED_TAGS:
        assert not re.search(rf'["\']{tag}["\']', source), f"{tag} aparece como literal de código, no solo mencionado en un comentario"


def test_allowed_message_tags_es_exactamente_los_dos_vigentes():
    assert ALLOWED_MESSAGE_TAGS == {"HUMAN_AGENT", "CUSTOMER_FEEDBACK"}


@pytest.mark.asyncio
async def test_dentro_de_ventana_usa_response_sin_tag(monkeypatch):
    captured = {}

    async def _fake_graph_fetch(path, *, body=None, **kwargs):
        captured["body"] = body
        return {"message_id": "mid.1"}

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    expires = datetime.now(UTC) + timedelta(hours=1)
    await send_messenger_message(
        "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(), service_window_expires_at=expires
    )
    assert captured["body"]["messaging_type"] == "RESPONSE"
    assert "tag" not in captured["body"]


@pytest.mark.asyncio
async def test_fuera_de_ventana_sin_confirmacion_falla_local_sin_llamar_a_meta(monkeypatch):
    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe llamar a graph_fetch sin confirmación humana")

    monkeypatch.setattr(messenger_mod, "graph_fetch", _debe_no_llamarse)

    with pytest.raises(MessengerSendError, match="agente humano"):
        await send_messenger_message(
            "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(), service_window_expires_at=None
        )


@pytest.mark.asyncio
async def test_fuera_de_ventana_sin_confirmacion_ventana_vencida_tambien_falla(monkeypatch):
    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe llamar a graph_fetch sin confirmación humana")

    monkeypatch.setattr(messenger_mod, "graph_fetch", _debe_no_llamarse)

    expired = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(MessengerSendError):
        await send_messenger_message(
            "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(), service_window_expires_at=expired
        )


@pytest.mark.asyncio
async def test_fuera_de_ventana_con_confirmacion_humana_usa_tag_human_agent(monkeypatch):
    captured = {}

    async def _fake_graph_fetch(path, *, body=None, **kwargs):
        captured["body"] = body
        return {"message_id": "mid.2"}

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    expired = datetime.now(UTC) - timedelta(hours=1)
    await send_messenger_message(
        "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(),
        service_window_expires_at=expired, human_confirmed=True,
    )
    assert captured["body"]["messaging_type"] == "MESSAGE_TAG"
    assert captured["body"]["tag"] == "HUMAN_AGENT"

"""Multimedia en el contrato de mensajes del panel (ADR-0196/0197)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core.api.routers import conversations as router_mod


def _mensaje(**campos: object) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "direction": "inbound",
        "sender_type": "customer",
        "content": "[Nota de voz]",
        "status": "sent",
        "created_at": datetime.now(UTC),
        "template_name": None,
        "error_reason": None,
        "turn_id": None,
        "media_type": "audio/ogg",
        "media_url": "demo/media/entrantes/audio.ogg",
        "media_transcription": "hola, busco un casco",
    }
    base.update(campos)
    return SimpleNamespace(**base)


async def test_message_item_expone_media_y_firma_la_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_mod, "url_firmada", lambda key: f"https://api/files/{key}")

    items = await router_mod._message_items(
        object(), uuid.uuid4(), uuid.uuid4(), [_mensaje()]
    )

    assert len(items) == 1
    item = items[0]
    assert item.media_type == "audio/ogg"
    assert item.media_url == "https://api/files/demo/media/entrantes/audio.ogg"
    assert item.media_transcription == "hola, busco un casco"


async def test_message_item_respeta_url_externa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_mod, "url_firmada", lambda key: f"https://api/files/{key}")

    items = await router_mod._message_items(
        object(),
        uuid.uuid4(),
        uuid.uuid4(),
        [_mensaje(media_type="image", media_url="https://cdn.example.com/foto.jpg", media_transcription=None)],
    )

    assert items[0].media_url == "https://cdn.example.com/foto.jpg"
    assert items[0].media_transcription is None


def test_media_url_ignora_keys_inseguras(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_mod, "url_firmada", lambda key: "no debería firmarse")

    assert router_mod._media_url(None) is None
    assert router_mod._media_url("../otro-tenant/secreto.ogg") is None

"""Publicación del inbound de `waweb` con media (ADR-0196)."""

from __future__ import annotations

import base64
import json
import uuid

import pytest

from core.waweb import inbound as inbound_mod


class FakeRedis:
    def __init__(self) -> None:
        self.sets: list[tuple[str, str, int | None]] = []
        self.xadds: list[tuple[str, dict[str, str]]] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.sets.append((key, value, ex))

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self.xadds.append((stream, fields))
        return "1-0"


@pytest.fixture()
def redis_falso(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    falso = FakeRedis()
    monkeypatch.setattr(inbound_mod, "get_redis", lambda: falso)

    async def _claim(*_a, **_k) -> bool:  # type: ignore[no-untyped-def]
        return True

    async def _con_cuota(*_a, **_k) -> bool:  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(inbound_mod, "claim_message_once", _claim)
    monkeypatch.setattr("core.billing.enforcement.check_metric", _con_cuota)
    return falso


async def test_audio_se_guarda_en_redis_y_al_stream_va_el_descriptor(
    redis_falso: FakeRedis,
) -> None:
    tid = uuid.uuid4()

    aceptado = await inbound_mod.publish_inbound(
        tenant_id=tid,
        connection_external_id="waweb:1",
        sender="573001112233",
        text="",
        wamid="W-AUDIO",
        media=[{"type": "audio/ogg", "data": b"voz", "seconds": 2, "ptt": True}],
    )

    assert aceptado is True
    assert len(redis_falso.sets) == 1
    clave, valor, ttl = redis_falso.sets[0]
    assert clave == f"waweb:media:{tid}:W-AUDIO:0"
    assert base64.b64decode(valor) == b"voz"
    assert ttl == inbound_mod._MEDIA_TTL_S

    _stream, fields = redis_falso.xadds[0]
    media = json.loads(fields["media"])
    assert media == [
        {
            "type": "audio/ogg",
            "seconds": 2,
            "ptt": True,
            "redis_key": f"waweb:media:{tid}:W-AUDIO:0",
            "bytes": 3,
        }
    ]


async def test_imagen_con_caption_se_publica_sin_bytes_en_el_stream(
    redis_falso: FakeRedis,
) -> None:
    tid = uuid.uuid4()

    await inbound_mod.publish_inbound(
        tenant_id=tid,
        connection_external_id="waweb:1",
        sender="573001112233",
        text="¿Tienen este?",
        wamid="W-IMG",
        media=[{"type": "image/jpeg", "data": b"foto", "caption": "¿Tienen este?"}],
    )

    media = json.loads(redis_falso.xadds[0][1]["media"])
    assert media[0]["redis_key"] == f"waweb:media:{tid}:W-IMG:0"
    assert "data" not in media[0]
    assert redis_falso.xadds[0][1]["content"] == "¿Tienen este?"


async def test_error_de_descarga_se_publica_sin_key(redis_falso: FakeRedis) -> None:
    await inbound_mod.publish_inbound(
        tenant_id=uuid.uuid4(),
        connection_external_id="waweb:1",
        sender="573001112233",
        text="",
        wamid="W-ERR",
        media=[{"type": "audio/ogg", "error": "download_failed"}],
    )

    media = json.loads(redis_falso.xadds[0][1]["media"])
    assert media[0]["error"] == "download_failed"
    assert "redis_key" not in media[0]
    assert redis_falso.sets == []


async def test_cuota_agotada_no_encola(
    redis_falso: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _sin_cuota(*_a, **_k) -> bool:  # type: ignore[no-untyped-def]
        return False

    monkeypatch.setattr("core.billing.enforcement.check_metric", _sin_cuota)

    aceptado = await inbound_mod.publish_inbound(
        tenant_id=uuid.uuid4(),
        connection_external_id="waweb:1",
        sender="573001112233",
        text="",
        wamid="W-CUOTA",
        media=[{"type": "audio/ogg", "data": b"voz"}],
    )

    assert aceptado is False
    assert redis_falso.xadds == []

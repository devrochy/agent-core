import json
import uuid
from types import SimpleNamespace

import pytest

from core.media import ingesta
from core.media.config import AudioConfig, VisionConfig
from core.media.meta_media import DownloadedMedia
from core.media.stt import TranscriptionResult
from core.media.vision import VisionResult
from core.shared.tenant import tenant_scope


def _app_settings(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "language": "es",
        "business_description": "Tienda de cascos y repuestos",
        "audio_enabled": True,
        "vision_enabled": True,
        "vision_mode": "describir",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _fields(media_type: str, content: str = "") -> dict[str, str]:
    return {
        "media": json.dumps([{"type": media_type, "id": "media-1", "url": ""}]),
        "content": content,
        "external_message_id": "wamid-1",
        "provider": "meta",
        "channel": "whatsapp",
    }


async def _preparar(monkeypatch: pytest.MonkeyPatch, media_type: str, *, caption: str = "") -> SimpleNamespace:
    tid = uuid.uuid4()

    async def _fake_credentials(*_a, **_k):  # type: ignore[no-untyped-def]
        return "meta", {"access_token": "token"}

    async def _fake_download(_media_id, _token):  # type: ignore[no-untyped-def]
        return DownloadedMedia(b"datos", media_type, "sha", 5)

    async def _fake_guardar(*_a, **_k):  # type: ignore[no-untyped-def]
        return "tenant-1/media/entrantes/archivo"

    async def _noop(*_a, **_k):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("core.gateway.sender.resolve_credentials", _fake_credentials)
    monkeypatch.setattr(ingesta, "descargar_media", _fake_download)
    monkeypatch.setattr(ingesta, "_guardar", _fake_guardar)
    monkeypatch.setattr(ingesta, "_cache_get", _noop)
    monkeypatch.setattr(ingesta, "_cache_set", _noop)
    monkeypatch.setattr(ingesta, "_registrar_consumo", _noop)
    monkeypatch.setattr(ingesta, "_respuesta", lambda key: _respuesta_fija(key))
    return SimpleNamespace(tid=tid, conversation=SimpleNamespace(id=uuid.uuid4(), connection_id=uuid.uuid4(), meta_connection_id=None), fields=_fields(media_type, caption))


async def _respuesta_fija(key: str) -> str:
    return ingesta.RESPUESTAS_FALLBACK.get(key, key)


async def test_audio_transcrito_sigue_como_texto(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await _preparar(monkeypatch, "audio/ogg")

    async def _fake_transcribir(*_a, **_k):  # type: ignore[no-untyped-def]
        return TranscriptionResult("hola, busco un casco", 2.0, 0.001, "groq", "whisper-large-v3-turbo")

    monkeypatch.setattr(ingesta, "resolve_audio_config", lambda _s: AudioConfig(True, "groq", "whisper-large-v3-turbo", "k", "platform"))
    monkeypatch.setattr(ingesta, "transcribir_audio", _fake_transcribir)

    async with tenant_scope(ctx.tid):
        outcome = await ingesta.procesar_media_entrante(ctx.fields, ctx.conversation, _app_settings())

    assert outcome.handled is True
    assert outcome.media_transcription == "hola, busco un casco"
    assert outcome.effective_message == "[Nota de voz] hola, busco un casco"
    assert outcome.content == "[Nota de voz]"
    assert outcome.reply is None


async def test_audio_ilegible_responde_sin_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await _preparar(monkeypatch, "audio/ogg")

    async def _fake_transcribir(*_a, **_k):  # type: ignore[no-untyped-def]
        return TranscriptionResult(None, 0, 0, "groq", "whisper-large-v3-turbo")

    monkeypatch.setattr(ingesta, "resolve_audio_config", lambda _s: AudioConfig(True, "groq", "whisper-large-v3-turbo", "k", "platform"))
    monkeypatch.setattr(ingesta, "transcribir_audio", _fake_transcribir)

    async with tenant_scope(ctx.tid):
        outcome = await ingesta.procesar_media_entrante(ctx.fields, ctx.conversation, _app_settings())

    assert outcome.reply_key == "media_audio_unreadable"
    assert outcome.reply
    assert outcome.media_transcription is None


async def test_imagen_se_describe(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await _preparar(monkeypatch, "image/jpeg", caption="¿Tienen este?")

    async def _fake_describir(*_a, **_k):  # type: ignore[no-untyped-def]
        return VisionResult('{"descripcion": "casco integral negro"}', 10, 5, 0.0001, "deepseek", "deepseek-flash")

    monkeypatch.setattr(
        ingesta,
        "resolve_vision_config",
        lambda _s: VisionConfig(True, "describir", "deepseek", "deepseek-flash", "k", "platform"),
    )
    monkeypatch.setattr(ingesta, "prompt_de_media", lambda *_a, **_k: "describí la imagen")
    monkeypatch.setattr(ingesta, "describir_imagen", _fake_describir)

    async with tenant_scope(ctx.tid):
        outcome = await ingesta.procesar_media_entrante(ctx.fields, ctx.conversation, _app_settings())

    assert outcome.media_transcription == "casco integral negro"
    assert "Descripción automática: casco integral negro" in outcome.effective_message
    assert outcome.effective_message.startswith("¿Tienen este?")
    assert outcome.content == "¿Tienen este?"


async def test_imagen_sin_descripcion_no_llama_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await _preparar(monkeypatch, "image/jpeg")
    llamado = False

    async def _fake_describir(*_a, **_k):  # type: ignore[no-untyped-def]
        nonlocal llamado
        llamado = True
        raise AssertionError("no debe llamarse")

    monkeypatch.setattr(ingesta, "describir_imagen", _fake_describir)

    async with tenant_scope(ctx.tid):
        outcome = await ingesta.procesar_media_entrante(
            ctx.fields, ctx.conversation, _app_settings(vision_enabled=False)
        )

    assert llamado is False
    assert outcome.decision == "imagen_sin_descripcion"
    assert "[Imagen adjunta]" in outcome.effective_message
    assert outcome.reply is None


async def test_imagen_no_identificada_responde_fijo(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await _preparar(monkeypatch, "image/jpeg")

    async def _fake_describir(*_a, **_k):  # type: ignore[no-untyped-def]
        return VisionResult('{"descripcion": null}', 10, 2, 0.0001, "deepseek", "deepseek-flash")

    monkeypatch.setattr(
        ingesta,
        "resolve_vision_config",
        lambda _s: VisionConfig(True, "describir", "deepseek", "deepseek-flash", "k", "platform"),
    )
    monkeypatch.setattr(ingesta, "prompt_de_media", lambda *_a, **_k: "describí la imagen")
    monkeypatch.setattr(ingesta, "describir_imagen", _fake_describir)

    async with tenant_scope(ctx.tid):
        outcome = await ingesta.procesar_media_entrante(ctx.fields, ctx.conversation, _app_settings())

    assert outcome.reply_key == "media_image_unidentified"
    assert outcome.reply

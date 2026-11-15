import json
import uuid
from types import SimpleNamespace

import pytest

from core.media import ingesta
from core.media.config import AudioConfig, VisionConfig
from core.media.errors import MediaProviderError
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


def _fields_waweb(
    media_type: str,
    *,
    tenant_id: uuid.UUID,
    caption: str = "",
    redis_key: str | None = None,
    error: str | None = None,
) -> dict[str, str]:
    item: dict[str, object] = {"type": media_type, "bytes": 5}
    if redis_key:
        item["redis_key"] = redis_key
    if error:
        item["error"] = error
    return {
        "media": json.dumps([item]),
        "content": caption,
        "external_message_id": "wamid-1",
        "provider": "waweb",
        "channel": "whatsapp",
    }


async def test_audio_waweb_transcrito_sin_credenciales_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = uuid.uuid4()

    async def _fake_waweb(_item, *, tenant_id):  # type: ignore[no-untyped-def]
        assert tenant_id == tid
        return DownloadedMedia(b"voz", "audio/ogg", "sha", 4)

    async def _sin_credenciales(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("waweb no debe resolver credenciales de Meta/Twilio")

    async def _guardar(*_a, **_k):  # type: ignore[no-untyped-def]
        return "tenant-1/media/entrantes/audio"

    async def _noop(*_a, **_k):  # type: ignore[no-untyped-def]
        return None

    async def _transcribir(*_a, **_k):  # type: ignore[no-untyped-def]
        return TranscriptionResult("hola, busco un casco", 2.0, 0.001, "openai", "gpt-4o-mini-transcribe")

    monkeypatch.setattr(ingesta, "descargar_media_waweb", _fake_waweb)
    monkeypatch.setattr("core.gateway.sender.resolve_credentials", _sin_credenciales)
    monkeypatch.setattr(ingesta, "_guardar", _guardar)
    monkeypatch.setattr(ingesta, "_cache_get", _noop)
    monkeypatch.setattr(ingesta, "_cache_set", _noop)
    monkeypatch.setattr(ingesta, "_registrar_consumo", _noop)
    monkeypatch.setattr(
        ingesta,
        "resolve_audio_config",
        lambda _s: AudioConfig(True, "openai", "gpt-4o-mini-transcribe", "sk", "platform"),
    )
    monkeypatch.setattr(ingesta, "transcribir_audio", _transcribir)

    fields = _fields_waweb(
        "audio/ogg",
        tenant_id=tid,
        redis_key=f"waweb:media:{tid}:wamid-1:0",
    )
    conversation = SimpleNamespace(id=uuid.uuid4(), connection_id=None, meta_connection_id=None)

    async with tenant_scope(tid):
        outcome = await ingesta.procesar_media_entrante(fields, conversation, _app_settings())

    assert outcome.handled is True
    assert outcome.media_transcription == "hola, busco un casco"
    assert outcome.media_key == "tenant-1/media/entrantes/audio"
    assert outcome.reply is None


async def test_media_waweb_expirada_responde_fijo(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = uuid.uuid4()

    async def _expirada(_item, *, tenant_id):  # type: ignore[no-untyped-def]
        raise MediaProviderError("expiro", code="media_expirada")

    async def _noop(*_a, **_k):  # type: ignore[no-untyped-def]
        return None

    async def _respuesta(key: str) -> str:
        return ingesta.RESPUESTAS_FALLBACK.get(key, key)

    monkeypatch.setattr(ingesta, "descargar_media_waweb", _expirada)
    monkeypatch.setattr(ingesta, "_cache_get", _noop)
    monkeypatch.setattr(ingesta, "_cache_set", _noop)
    monkeypatch.setattr(ingesta, "_respuesta", _respuesta)

    fields = _fields_waweb(
        "audio/ogg",
        tenant_id=tid,
        redis_key=f"waweb:media:{tid}:wamid-1:0",
    )
    conversation = SimpleNamespace(id=uuid.uuid4(), connection_id=None, meta_connection_id=None)

    async with tenant_scope(tid):
        outcome = await ingesta.procesar_media_entrante(fields, conversation, _app_settings())

    assert outcome.handled is True
    assert outcome.reply_key == "media_download_failed"
    assert outcome.decision == "media_descarga_fallida"

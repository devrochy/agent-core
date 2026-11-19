import uuid
from types import SimpleNamespace

import pytest

from core.api.routers import settings as settings_router
from core.media.config import MediaConfigError


def _principal() -> SimpleNamespace:
    return SimpleNamespace(tenant_id=uuid.uuid4())


def _media_data() -> dict:
    return {
        "audio": {
            "enabled": True,
            "provider": "groq",
            "model": "whisper-large-v3-turbo",
            "has_api_key": False,
            "using_platform_key": True,
        },
        "vision": {
            "enabled": True,
            "mode": "describir",
            "provider": "deepseek",
            "model": "deepseek-flash",
            "has_api_key": False,
            "using_platform_key": True,
        },
        "catalog": {
            "audio": [
                {
                    "key": "groq",
                    "label": "Groq · Whisper",
                    "default_model": "whisper-large-v3-turbo",
                    "models": [{"id": "whisper-large-v3-turbo", "label": "Turbo"}],
                }
            ],
            "vision": [
                {
                    "key": "deepseek",
                    "label": "DeepSeek · visión",
                    "default_model": "deepseek-flash",
                    "models": [{"id": "deepseek-flash", "label": "Flash"}],
                }
            ],
        },
    }


async def test_get_media_settings_serializa_el_contrato(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_read(_tenant_id):  # type: ignore[no-untyped-def]
        return _media_data()

    monkeypatch.setattr("core.media.config.read_media_config", _fake_read)
    out = await settings_router.get_media_settings(_principal())  # type: ignore[arg-type]
    assert out.audio.provider == "groq"
    assert out.vision.mode == "describir"
    assert out.catalog.audio[0].models[0].id == "whisper-large-v3-turbo"


async def test_save_audio_devuelve_el_estado_efectivo(monkeypatch: pytest.MonkeyPatch) -> None:
    guardado: dict = {}

    async def _fake_save(tenant_id, **kwargs):  # type: ignore[no-untyped-def]
        guardado["tenant_id"] = tenant_id
        guardado.update(kwargs)

    async def _fake_read(_tenant_id):  # type: ignore[no-untyped-def]
        return _media_data()

    monkeypatch.setattr("core.media.config.save_audio_config", _fake_save)
    monkeypatch.setattr("core.media.config.read_media_config", _fake_read)
    payload = settings_router.MediaAudioUpdate(
        enabled=True, provider="groq", model="whisper-large-v3-turbo", api_key="gsk_x"
    )
    out = await settings_router.save_media_audio(payload, _principal())  # type: ignore[arg-type]
    assert guardado["provider"] == "groq"
    assert guardado["api_key"] == "gsk_x"
    assert out.provider == "groq"


async def test_save_audio_traduce_error_de_config(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_save(*_a, **_k):  # type: ignore[no-untyped-def]
        raise MediaConfigError("audio_provider_invalido", "Proveedor de audio inválido.")

    monkeypatch.setattr("core.media.config.save_audio_config", _fake_save)
    payload = settings_router.MediaAudioUpdate(enabled=True, provider="otro")
    with pytest.raises(Exception) as exc:
        await settings_router.save_media_audio(payload, _principal())  # type: ignore[arg-type]
    assert getattr(exc.value, "code", None) == "audio_provider_invalido"


async def test_test_vision_traduce_error_del_proveedor(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.media.errors import MediaProviderError

    async def _fake_test(*_a, **_k):  # type: ignore[no-untyped-def]
        raise MediaProviderError("sin llave", code="vision_sin_llave")

    monkeypatch.setattr("core.media.config.test_vision_config", _fake_test)
    payload = settings_router.MediaTestRequest(provider="deepseek", model="deepseek-flash")
    with pytest.raises(Exception) as exc:
        await settings_router.test_media_vision(payload, _principal())  # type: ignore[arg-type]
    assert getattr(exc.value, "code", None) == "vision_sin_llave"

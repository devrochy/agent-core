import uuid
from types import SimpleNamespace

import pytest

from core.config import get_settings
from core.media.config import (
    MediaConfigError,
    resolve_audio_config,
    resolve_vision_config,
)
from core.shared.crypto import get_secret_box


@pytest.fixture(autouse=True)
def _settings_frescas():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "audio_enabled": True,
        "audio_provider": None,
        "audio_model": None,
        "audio_api_key_encrypted": None,
        "vision_enabled": True,
        "vision_mode": "describir",
        "vision_provider": None,
        "vision_model": None,
        "vision_api_key_encrypted": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_audio_usa_groq_y_llave_de_plataforma(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_PROVIDER", "groq")
    monkeypatch.setenv("STT_MODEL", "whisper-large-v3-turbo")
    monkeypatch.setenv("STT_API_KEY", "gsk_plataforma")
    cfg = resolve_audio_config(None)
    assert cfg.provider == "groq"
    assert cfg.model == "whisper-large-v3-turbo"
    assert cfg.api_key == "gsk_plataforma"
    assert cfg.key_mode == "platform"


def test_audio_byok_gana_sobre_la_plataforma(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_API_KEY", "gsk_plataforma")
    encrypted = get_secret_box().encrypt("gsk_propia")
    cfg = resolve_audio_config(_settings(audio_provider="groq", audio_api_key_encrypted=encrypted))
    assert cfg.api_key == "gsk_propia"
    assert cfg.key_mode == "byok"


def test_audio_sin_llave_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in ("STT_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(nombre, raising=False)
    with pytest.raises(MediaConfigError) as exc:
        resolve_audio_config(None)
    assert exc.value.code == "audio_sin_llave"


def test_vision_usa_deepseek_por_defecto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISION_PROVIDER", "deepseek")
    monkeypatch.setenv("VISION_API_KEY", "sk_plataforma")
    cfg = resolve_vision_config(None)
    assert cfg.provider == "deepseek"
    assert cfg.model == "deepseek-flash"
    assert cfg.mode == "describir"
    assert cfg.key_mode == "platform"


def test_vision_byok_y_modo_sin_descripcion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISION_API_KEY", "sk_plataforma")
    encrypted = get_secret_box().encrypt("sk_propia")
    cfg = resolve_vision_config(
        _settings(
            vision_provider="deepseek",
            vision_mode="sin_descripcion",
            vision_api_key_encrypted=encrypted,
        )
    )
    assert cfg.key_mode == "byok"
    assert cfg.mode == "sin_descripcion"


async def test_read_media_config_no_expone_llaves(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.media import config as media_config

    async def _fake_app_settings(_tenant_id):  # type: ignore[no-untyped-def]
        return _settings(
            audio_provider="groq",
            audio_api_key_encrypted=get_secret_box().encrypt("gsk_secreta"),
            vision_provider="deepseek",
            vision_api_key_encrypted=get_secret_box().encrypt("sk_secreta"),
        )

    monkeypatch.setattr(media_config, "_app_settings", _fake_app_settings)
    datos = await media_config.read_media_config(uuid.uuid4())
    assert datos["audio"]["has_api_key"] is True
    assert datos["vision"]["has_api_key"] is True
    assert "gsk_secreta" not in str(datos)
    assert "sk_secreta" not in str(datos)
    assert {p["key"] for p in datos["catalog"]["audio"]} == {"groq", "openai"}
    assert {p["key"] for p in datos["catalog"]["vision"]} == {"deepseek", "anthropic", "openai"}

"""Configuración y resolución de proveedores de media (SPEC-04, ADR-0151).

Precedencia: configuración del negocio en `app_settings` → fallback de plataforma
en `.env`. La llave propia se descifra solo en el momento de usarla y nunca se
devuelve por la API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from core.config import get_settings
from core.db.models import AppSettings
from core.db.session import get_session
from core.media.catalog import (
    AUDIO_CATALOG,
    VISION_CATALOG,
    AudioProviderKey,
    VisionProviderKey,
    audio_model_or_default,
    is_audio_provider,
    is_vision_mode,
    is_vision_provider,
    vision_model_or_default,
)
from core.shared.crypto import get_secret_box

KeyMode = Literal["platform", "byok"]


class MediaConfigError(ValueError):
    """No hay proveedor utilizable. `code` va al log/API; el mensaje es para el dueño."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AudioConfig:
    enabled: bool
    provider: AudioProviderKey
    model: str
    api_key: str
    key_mode: KeyMode


@dataclass(frozen=True, slots=True)
class VisionConfig:
    enabled: bool
    mode: str
    provider: VisionProviderKey
    model: str
    api_key: str
    key_mode: KeyMode


async def _app_settings(tenant_id: uuid.UUID) -> AppSettings | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


def _tenant_key(encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    try:
        return get_secret_box().decrypt(encrypted) or None
    except Exception as exc:  # configuración cifrada con otra clave / dato corrupto
        raise MediaConfigError(
            "media_key_invalida",
            "La llave guardada del proveedor multimedia no se pudo descifrar.",
        ) from exc


def _platform_audio_key(provider: AudioProviderKey) -> str:
    settings = get_settings()
    # `STT_API_KEY` es la llave de plataforma del proveedor activo; si está vacía,
    # se usa la llave específica del proveedor.
    if settings.stt_api_key:
        return settings.stt_api_key
    if provider == "groq":
        return settings.groq_api_key
    return settings.openai_api_key


def _platform_vision_key(provider: VisionProviderKey) -> str:
    settings = get_settings()
    if settings.vision_api_key:
        return settings.vision_api_key
    if provider == "deepseek":
        return settings.deepseek_api_key or settings.llm_api_key
    if provider == "anthropic":
        return settings.anthropic_api_key
    return settings.openai_api_key


def resolve_audio_config(app_settings: AppSettings | None) -> AudioConfig:
    """Resuelve el proveedor de audio del negocio (o falla con `MediaConfigError`)."""
    settings = get_settings()
    tenant_provider = app_settings.audio_provider if app_settings else None
    provider: AudioProviderKey = (
        tenant_provider if is_audio_provider(tenant_provider) else settings.stt_provider
    )
    if not is_audio_provider(provider):  # defensa ante un env inválido
        raise MediaConfigError("audio_provider_invalido", f"Proveedor de audio desconocido: {provider}.")

    tenant_model = app_settings.audio_model if app_settings else None
    model = audio_model_or_default(
        provider, tenant_model or (settings.stt_model if provider == settings.stt_provider else None)
    )
    own_key = _tenant_key(app_settings.audio_api_key_encrypted if app_settings else None)
    api_key = own_key or _platform_audio_key(provider)
    if not api_key:
        raise MediaConfigError(
            "audio_sin_llave",
            f"El proveedor de audio «{AUDIO_CATALOG[provider].label}» no tiene llave configurada.",
        )
    return AudioConfig(
        enabled=bool(app_settings.audio_enabled) if app_settings else True,
        provider=provider,
        model=model,
        api_key=api_key,
        key_mode="byok" if own_key else "platform",
    )


def resolve_vision_config(app_settings: AppSettings | None) -> VisionConfig:
    """Resuelve el proveedor de visión del negocio (o falla con `MediaConfigError`)."""
    settings = get_settings()
    tenant_provider = app_settings.vision_provider if app_settings else None
    provider: VisionProviderKey = (
        tenant_provider if is_vision_provider(tenant_provider) else settings.vision_provider
    )
    if not is_vision_provider(provider):
        raise MediaConfigError(
            "vision_provider_invalido", f"Proveedor de visión desconocido: {provider}."
        )

    tenant_model = app_settings.vision_model if app_settings else None
    model = vision_model_or_default(
        provider,
        tenant_model or (settings.vision_model if provider == settings.vision_provider else None),
    )
    own_key = _tenant_key(app_settings.vision_api_key_encrypted if app_settings else None)
    api_key = own_key or _platform_vision_key(provider)
    if not api_key:
        raise MediaConfigError(
            "vision_sin_llave",
            f"El proveedor de visión «{VISION_CATALOG[provider].label}» no tiene llave configurada.",
        )
    mode = app_settings.vision_mode if app_settings else "describir"
    return VisionConfig(
        enabled=bool(app_settings.vision_enabled) if app_settings else True,
        mode=mode if is_vision_mode(mode) else "describir",
        provider=provider,
        model=model,
        api_key=api_key,
        key_mode="byok" if own_key else "platform",
    )


def _display_audio(app_settings: AppSettings | None) -> dict[str, Any]:
    settings = get_settings()
    provider = (
        app_settings.audio_provider
        if app_settings and is_audio_provider(app_settings.audio_provider)
        else settings.stt_provider
    )
    model = audio_model_or_default(
        provider,
        (app_settings.audio_model if app_settings else None)
        or (settings.stt_model if provider == settings.stt_provider else None),
    )
    own_key = bool(app_settings and app_settings.audio_api_key_encrypted)
    return {
        "enabled": bool(app_settings.audio_enabled) if app_settings else True,
        "provider": provider,
        "model": model,
        "has_api_key": own_key,
        "using_platform_key": not own_key and bool(_platform_audio_key(provider)),
    }


def _display_vision(app_settings: AppSettings | None) -> dict[str, Any]:
    settings = get_settings()
    provider = (
        app_settings.vision_provider
        if app_settings and is_vision_provider(app_settings.vision_provider)
        else settings.vision_provider
    )
    model = vision_model_or_default(
        provider,
        (app_settings.vision_model if app_settings else None)
        or (settings.vision_model if provider == settings.vision_provider else None),
    )
    own_key = bool(app_settings and app_settings.vision_api_key_encrypted)
    return {
        "enabled": bool(app_settings.vision_enabled) if app_settings else True,
        "mode": (app_settings.vision_mode if app_settings else "describir") or "describir",
        "provider": provider,
        "model": model,
        "has_api_key": own_key,
        "using_platform_key": not own_key and bool(_platform_vision_key(provider)),
    }


async def read_media_config(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Estado para el panel: configuración efectiva + catálogo, sin llaves."""
    app_settings = await _app_settings(tenant_id)
    return {
        "audio": _display_audio(app_settings),
        "vision": _display_vision(app_settings),
        "catalog": {
            "audio": [
                {
                    "key": entry.key,
                    "label": entry.label,
                    "default_model": entry.default_model,
                    "models": [{"id": m.id, "label": m.label} for m in entry.models],
                }
                for entry in AUDIO_CATALOG.values()
            ],
            "vision": [
                {
                    "key": entry.key,
                    "label": entry.label,
                    "default_model": entry.default_model,
                    "models": [{"id": m.id, "label": m.label} for m in entry.models],
                }
                for entry in VISION_CATALOG.values()
            ],
        },
    }


async def save_audio_config(
    tenant_id: uuid.UUID,
    *,
    enabled: bool,
    provider: str,
    model: str,
    api_key: str = "",
) -> None:
    if not is_audio_provider(provider):
        raise MediaConfigError("audio_provider_invalido", "Proveedor de audio inválido.")
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=tenant_id)
            session.add(app_settings)
        app_settings.audio_enabled = enabled
        app_settings.audio_provider = provider
        app_settings.audio_model = audio_model_or_default(provider, model)
        if api_key:
            app_settings.audio_api_key_encrypted = get_secret_box().encrypt(api_key)


async def save_vision_config(
    tenant_id: uuid.UUID,
    *,
    enabled: bool,
    mode: str,
    provider: str,
    model: str,
    api_key: str = "",
) -> None:
    if not is_vision_provider(provider):
        raise MediaConfigError("vision_provider_invalido", "Proveedor de visión inválido.")
    if not is_vision_mode(mode):
        raise MediaConfigError("vision_mode_invalido", "Modo de imagen inválido.")
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=tenant_id)
            session.add(app_settings)
        app_settings.vision_enabled = enabled
        app_settings.vision_mode = mode
        app_settings.vision_provider = provider
        app_settings.vision_model = vision_model_or_default(provider, model)
        if api_key:
            app_settings.vision_api_key_encrypted = get_secret_box().encrypt(api_key)


async def test_audio_config(
    tenant_id: uuid.UUID,
    *,
    provider: str,
    model: str,
    api_key: str = "",
) -> None:
    """Prueba la combinación antes de guardarla. No persiste nada."""
    from core.media.stt import transcribir_audio

    if not is_audio_provider(provider):
        raise MediaConfigError("audio_provider_invalido", "Proveedor de audio inválido.")
    app_settings = await _app_settings(tenant_id)
    key = api_key or (
        _tenant_key(app_settings.audio_api_key_encrypted if app_settings else None)
        or _platform_audio_key(provider)
    )
    if not key:
        raise MediaConfigError("audio_sin_llave", "No hay una llave para probar este proveedor.")
    resolved_model = audio_model_or_default(provider, model)
    # Silencio OGG/Opus de 1 s: el 200 confirma endpoint/llave/modelo aunque el texto venga vacío.
    from core.media.stt import PRUEBA_AUDIO_OGG

    await transcribir_audio(
        PRUEBA_AUDIO_OGG,
        "audio/ogg",
        provider=provider,
        model=resolved_model,
        api_key=key,
        language="es",
    )


async def test_vision_config(
    tenant_id: uuid.UUID,
    *,
    provider: str,
    model: str,
    api_key: str = "",
) -> None:
    """Prueba la combinación antes de guardarla. No persiste nada."""
    from core.media.vision import PRUEBA_IMAGEN_PNG, describir_imagen

    if not is_vision_provider(provider):
        raise MediaConfigError("vision_provider_invalido", "Proveedor de visión inválido.")
    app_settings = await _app_settings(tenant_id)
    key = api_key or (
        _tenant_key(app_settings.vision_api_key_encrypted if app_settings else None)
        or _platform_vision_key(provider)
    )
    if not key:
        raise MediaConfigError("vision_sin_llave", "No hay una llave para probar este proveedor.")
    await describir_imagen(
        PRUEBA_IMAGEN_PNG,
        "image/png",
        provider=provider,
        model=vision_model_or_default(provider, model),
        api_key=key,
        prompt="Respondé únicamente con la palabra: ok",
        max_tokens=16,
    )


__all__ = [
    "AudioConfig",
    "KeyMode",
    "MediaConfigError",
    "VisionConfig",
    "read_media_config",
    "resolve_audio_config",
    "resolve_vision_config",
    "save_audio_config",
    "save_vision_config",
    "test_audio_config",
    "test_vision_config",
]

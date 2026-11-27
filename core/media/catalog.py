"""Catálogo de proveedores de audio (STT) y visión (SPEC-04, ADR-0151).

Es la única fuente de verdad de modelos, URLs, calidad y precios de referencia de
las dos capacidades. El panel lo expone para poblar las opciones de calidad/costo;
el resolver lo usa para validar la configuración guardada.

`quality` es una escala de 1 a 5 que el equipo define por capacidad y revisa cuando
entra o sale un modelo. `tier` es la identidad semántica de la opción (económico,
equilibrado, rápido, preciso, detallado, máxima); el panel la usa para elegir el
nombre, la descripción y el icono sin mostrar el modelo. `legacy` conserva un modelo
válido para configuraciones guardadas, pero no se ofrece en el panel.

Los precios son de referencia para el ledger en modo sombra; un cambio de precio no
reescribe filas viejas (cada fila guarda su costo).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AudioProviderKey = Literal["groq", "openai"]
VisionProviderKey = Literal["deepseek", "anthropic", "openai"]
VisionMode = Literal["describir", "sin_descripcion"]

# Referencia para estimar el costo por imagen sin tokens reales. El ledger usa los
# tokens que informa el proveedor; esto es solo para la etiqueta de la opción.
VISION_REF_INPUT_TOKENS = 1_200
VISION_REF_OUTPUT_TOKENS = 300


@dataclass(frozen=True, slots=True)
class ModelOption:
    id: str
    label: str
    # 1 = no sirve · 2 = simple · 3 = uso diario · 4 = difícil · 5 = máxima.
    quality: int
    # Identidad de la opción para el panel; no es el nombre del modelo.
    tier: str
    # Válido para configuraciones guardadas, pero no se ofrece como opción nueva.
    legacy: bool = False
    # Se ofrece en el panel. `False` retira un modelo vigente pero dominado en costo/calidad.
    offered: bool = True


@dataclass(frozen=True, slots=True)
class AudioProvider:
    key: AudioProviderKey
    label: str
    base_url: str
    default_model: str
    models: tuple[ModelOption, ...]
    # USD por minuto de audio (para el costo en sombra).
    usd_per_minute: dict[str, float]
    key_placeholder: str


@dataclass(frozen=True, slots=True)
class VisionProvider:
    key: VisionProviderKey
    label: str
    family: Literal["anthropic", "openai_compatible"]
    base_url: str
    default_model: str
    models: tuple[ModelOption, ...]
    # USD por millón de tokens (entrada, salida).
    usd_per_mtok: dict[str, tuple[float, float]]
    key_placeholder: str


AUDIO_CATALOG: dict[AudioProviderKey, AudioProvider] = {
    "groq": AudioProvider(
        key="groq",
        label="Groq · Whisper",
        base_url="https://api.groq.com/openai/v1",
        default_model="whisper-large-v3-turbo",
        models=(
            ModelOption(
                "whisper-large-v3-turbo",
                "Whisper Large v3 Turbo · rápido",
                quality=4,
                tier="rapido",
            ),
            ModelOption(
                "whisper-large-v3",
                "Whisper Large v3 · preciso",
                quality=4,
                tier="precision",
            ),
        ),
        usd_per_minute={
            "whisper-large-v3-turbo": 0.04 / 60,
            "whisper-large-v3": 0.111 / 60,
        },
        key_placeholder="gsk_…",
    ),
    "openai": AudioProvider(
        key="openai",
        label="OpenAI · Whisper",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini-transcribe",
        models=(
            ModelOption(
                "gpt-4o-mini-transcribe",
                "GPT-4o mini Transcribe · básico (no se ofrece)",
                quality=3,
                tier="basico",
                offered=False,
            ),
            ModelOption(
                "gpt-4o-transcribe",
                "GPT-4o Transcribe · máxima",
                quality=5,
                tier="maxima",
            ),
            ModelOption(
                "whisper-1",
                "Whisper-1 · clásico (legado)",
                quality=2,
                tier="legado",
                legacy=True,
                offered=False,
            ),
        ),
        usd_per_minute={
            "gpt-4o-mini-transcribe": 0.003,
            "gpt-4o-transcribe": 0.006,
            "whisper-1": 0.006,
        },
        key_placeholder="sk-…",
    ),
}

VISION_CATALOG: dict[VisionProviderKey, VisionProvider] = {
    "deepseek": VisionProvider(
        key="deepseek",
        label="DeepSeek · visión",
        family="openai_compatible",
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
        models=(
            ModelOption(
                "deepseek-flash",
                "DeepSeek Flash · económico y multimodal",
                quality=2,
                tier="economico",
            ),
        ),
        usd_per_mtok={"deepseek-flash": (0.14, 0.28)},
        key_placeholder="sk-…",
    ),
    "anthropic": VisionProvider(
        key="anthropic",
        label="Claude · visión",
        family="anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-haiku-4-5",
        models=(
            ModelOption(
                "claude-haiku-4-5",
                "Claude Haiku 4.5 · rápido y económico",
                quality=4,
                tier="detallado",
            ),
            ModelOption(
                "claude-sonnet-4-5",
                "Claude Sonnet 4.5 · más preciso",
                quality=5,
                tier="maxima",
            ),
        ),
        usd_per_mtok={
            "claude-haiku-4-5": (1.00, 5.00),
            "claude-sonnet-4-5": (3.00, 15.00),
        },
        key_placeholder="sk-ant-…",
    ),
    "openai": VisionProvider(
        key="openai",
        label="OpenAI · visión",
        family="openai_compatible",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=(
            ModelOption(
                "gpt-4o-mini",
                "GPT-4o mini · económico",
                quality=3,
                tier="equilibrado",
            ),
            ModelOption(
                "gpt-4.1-mini",
                "GPT-4.1 mini · preciso",
                quality=4,
                tier="preciso",
            ),
            ModelOption(
                "gpt-4o",
                "GPT-4o · detallado (legado)",
                quality=4,
                tier="detallado",
                legacy=True,
            ),
        ),
        usd_per_mtok={
            "gpt-4o-mini": (0.15, 0.60),
            "gpt-4.1-mini": (0.40, 1.60),
            "gpt-4o": (2.50, 10.00),
        },
        key_placeholder="sk-…",
    ),
}


# Voz de salida (TTS, ADR-0197, pack-voice): USD por millón de caracteres del texto sintetizado.
# Fuente: https://developers.openai.com/api/docs/pricing (2026-11-22): `tts-1` USD 15 y `tts-1-hd`
# USD 30 por 1M de caracteres. `gpt-4o-mini-tts` se cobra por tokens (USD 0,60 por 1M de texto de
# entrada y USD 12 por 1M de audio de salida) y OpenAI no publica su precio por carácter: se usa el
# de `tts-1` como estimación hasta calibrarlo con el uso real (HU-BILL-29). Un modelo que no esté
# aquí se costea con el precio más alto (`tts-1-hd`): cobrar de menos sería regalar el gasto.
TTS_USD_POR_MILLON_DE_CARACTERES: dict[str, float] = {
    "tts-1": 15.0,
    "tts-1-hd": 30.0,
    "gpt-4o-mini-tts": 15.0,
}
_TTS_PRECIO_DESCONOCIDO = 30.0


def tts_cost_usd(model: str, caracteres: int) -> float:
    """Costo aproximado de sintetizar `caracteres` con `model` (OpenAI)."""
    if caracteres <= 0:
        return 0.0
    tarifa = TTS_USD_POR_MILLON_DE_CARACTERES.get(model, _TTS_PRECIO_DESCONOCIDO)
    return round(tarifa * caracteres / 1_000_000, 6)


def is_audio_provider(value: str | None) -> bool:
    return bool(value) and value in AUDIO_CATALOG


def is_vision_provider(value: str | None) -> bool:
    return bool(value) and value in VISION_CATALOG


def is_vision_mode(value: str | None) -> bool:
    return value in ("describir", "sin_descripcion")


def audio_model_or_default(provider: AudioProviderKey, model: str | None) -> str:
    entry = AUDIO_CATALOG[provider]
    return model if model in {m.id for m in entry.models} else entry.default_model


def vision_model_or_default(provider: VisionProviderKey, model: str | None) -> str:
    entry = VISION_CATALOG[provider]
    return model if model in {m.id for m in entry.models} else entry.default_model


def audio_cost_usd(provider: AudioProviderKey, model: str, duration_s: float) -> float:
    """Costo aproximado del audio. `duration_s` puede ser 0 si el proveedor no la informa."""
    entry = AUDIO_CATALOG[provider]
    rate = entry.usd_per_minute.get(model)
    if rate is None or duration_s <= 0:
        return 0.0
    return round(rate * (duration_s / 60.0), 6)


def vision_cost_usd(
    provider: VisionProviderKey, model: str, input_tokens: int, output_tokens: int
) -> float:
    entry = VISION_CATALOG[provider]
    entrada, salida = entry.usd_per_mtok.get(model, (0.0, 0.0))
    return round((input_tokens / 1_000_000) * entrada + (output_tokens / 1_000_000) * salida, 6)


def audio_price_per_minute(provider: AudioProviderKey, model: str) -> float:
    """Precio de referencia por minuto, tal como lo publica el proveedor."""
    return round(AUDIO_CATALOG[provider].usd_per_minute.get(model, 0.0), 6)


def vision_price_per_image(
    provider: VisionProviderKey,
    model: str,
    input_tokens: int = VISION_REF_INPUT_TOKENS,
    output_tokens: int = VISION_REF_OUTPUT_TOKENS,
) -> float:
    """Estimado de referencia por imagen con los tokens típicos de una foto."""
    entrada, salida = VISION_CATALOG[provider].usd_per_mtok.get(model, (0.0, 0.0))
    return round(
        (input_tokens / 1_000_000) * entrada + (output_tokens / 1_000_000) * salida,
        6,
    )


__all__ = [
    "AUDIO_CATALOG",
    "TTS_USD_POR_MILLON_DE_CARACTERES",
    "VISION_CATALOG",
    "VISION_REF_INPUT_TOKENS",
    "VISION_REF_OUTPUT_TOKENS",
    "AudioProviderKey",
    "ModelOption",
    "VisionMode",
    "VisionProviderKey",
    "audio_cost_usd",
    "audio_model_or_default",
    "audio_price_per_minute",
    "is_audio_provider",
    "is_vision_mode",
    "is_vision_provider",
    "tts_cost_usd",
    "vision_cost_usd",
    "vision_model_or_default",
    "vision_price_per_image",
]

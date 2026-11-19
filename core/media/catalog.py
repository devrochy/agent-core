"""Catálogo de proveedores de audio (STT) y visión (SPEC-04, ADR-0151).

Es la única fuente de verdad de modelos, URLs y precios de referencia de las dos
capacidades. El panel lo expone para poblar los selects y el resolver lo usa para
validar la configuración guardada.

Los precios son de referencia para el ledger en modo sombra; un cambio de precio
no reescribe filas viejas (cada fila guarda su costo).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AudioProviderKey = Literal["groq", "openai"]
VisionProviderKey = Literal["deepseek", "anthropic", "openai"]
VisionMode = Literal["describir", "sin_descripcion"]


@dataclass(frozen=True, slots=True)
class ModelOption:
    id: str
    label: str


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
            ModelOption("whisper-large-v3-turbo", "Whisper Large v3 Turbo · el más rápido y barato"),
            ModelOption("whisper-large-v3", "Whisper Large v3 · más preciso"),
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
            ModelOption("gpt-4o-mini-transcribe", "GPT-4o mini Transcribe · económico"),
            ModelOption("whisper-1", "Whisper-1 · clásico"),
        ),
        usd_per_minute={
            "gpt-4o-mini-transcribe": 0.003,
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
        models=(ModelOption("deepseek-flash", "DeepSeek Flash · económico y multimodal"),),
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
            ModelOption("claude-haiku-4-5", "Claude Haiku 4.5 · rápido y económico"),
            ModelOption("claude-sonnet-4-5", "Claude Sonnet 4.5 · más preciso"),
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
            ModelOption("gpt-4o-mini", "GPT-4o mini · económico"),
            ModelOption("gpt-4o", "GPT-4o · más preciso"),
        ),
        usd_per_mtok={
            "gpt-4o-mini": (0.15, 0.60),
            "gpt-4o": (2.50, 10.00),
        },
        key_placeholder="sk-…",
    ),
}


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


__all__ = [
    "AUDIO_CATALOG",
    "VISION_CATALOG",
    "AudioProviderKey",
    "ModelOption",
    "VisionMode",
    "VisionProviderKey",
    "audio_cost_usd",
    "audio_model_or_default",
    "is_audio_provider",
    "is_vision_mode",
    "is_vision_provider",
    "vision_cost_usd",
    "vision_model_or_default",
]

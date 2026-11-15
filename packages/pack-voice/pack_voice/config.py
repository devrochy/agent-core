"""Configuración del pack de respuestas por voz (ADR-0197)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr

from core.mcp.types import BasePluginConfig


class VoiceConfig(BasePluginConfig):
    enabled: bool = Field(
        default=True,
        description="Responder con notas de voz cuando la política lo decida.",
    )
    modo: Literal["mixto", "siempre", "nunca"] = Field(
        default="mixto",
        description="'mixto' sortea con probabilidad_voz; 'siempre' responde con voz; 'nunca' solo texto.",
    )
    solo_si_cliente_manda_audio: bool = Field(
        default=True,
        description="Solo responder con voz si el último mensaje del cliente fue una nota de voz.",
    )
    probabilidad_voz: int = Field(
        default=50,
        ge=0,
        le=100,
        description="Porcentaje de turnos que responden con voz en modo mixto.",
    )
    evitar_voz_seguida: bool = Field(
        default=True,
        description="Si la respuesta anterior fue una nota de voz, esta sale solo en texto.",
    )
    incluir_texto: bool = Field(
        default=True,
        description="Cuando se elige voz, enviar primero el texto y después la nota de voz.",
    )
    max_caracteres: int = Field(
        default=600,
        ge=50,
        le=4000,
        description="Respuestas más largas que este tope salen solo en texto.",
    )
    provider: Literal["openai"] = Field(
        default="openai",
        description="Proveedor de síntesis de voz.",
    )
    model: str = Field(
        default="tts-1",
        description="Modelo de voz de OpenAI (tts-1, tts-1-hd o gpt-4o-mini-tts).",
    )
    voice: str = Field(
        default="alloy",
        description="Voz de OpenAI (alloy, echo, fable, onyx, nova, shimmer…).",
    )
    speed: float = Field(
        default=1.0,
        ge=0.25,
        le=4.0,
        description="Velocidad de la voz (0.25 a 4.0).",
    )
    api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Llave propia de OpenAI (BYOK). Vacío = llave de plataforma.",
    )

"""Síntesis de voz de salida con OpenAI (ADR-0197).

Vive en el core (como el STT y la visión) para que un pack no importe un cliente
HTTP: `pack-voice` aporta la política, la configuración y el guardado; acá queda
la llamada al proveedor. Devuelve el audio tal como lo entrega OpenAI (Ogg/Opus
con `response_format="opus"`).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.config import get_settings
from core.media.errors import MediaProviderError, error_from_status
from core.shared.logger import get_logger

log = get_logger(__name__)

OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech"


@dataclass(frozen=True, slots=True)
class SpeechResult:
    contenido: bytes
    mimetype: str = "audio/ogg"


async def sintetizar_audio(
    texto: str,
    *,
    model: str,
    voice: str,
    api_key: str,
    speed: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SpeechResult:
    """Pide la nota de voz a OpenAI. Lanza `MediaProviderError` si falla."""
    if not api_key:
        raise MediaProviderError("Falta la llave de OpenAI para sintetizar voz.", code="tts_sin_llave")
    if not texto.strip():
        raise MediaProviderError("No hay texto para sintetizar.", code="tts_texto_vacio")

    payload = {
        "model": model,
        "voice": voice,
        "input": texto[:4000],
        "response_format": "opus",
        "speed": speed,
    }
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().tts_timeout_s, transport=transport
        ) as client:
            response = await client.post(
                OPENAI_SPEECH_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise MediaProviderError(
            "OpenAI no respondió a tiempo al sintetizar la voz.",
            transient=True,
            code="tts_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaProviderError(
            f"No se pudo conectar con OpenAI para sintetizar voz: {type(exc).__name__}",
            transient=True,
            code="tts_red",
        ) from exc

    if response.status_code >= 400:
        raise error_from_status(response.status_code, response.text)
    if not response.content:
        raise MediaProviderError("OpenAI devolvió un audio vacío.", code="tts_vacio")
    log.info("tts_generada", provider="openai", model=model, voice=voice, bytes=len(response.content))
    return SpeechResult(contenido=response.content)


__all__ = ["OPENAI_SPEECH_URL", "SpeechResult", "sintetizar_audio"]

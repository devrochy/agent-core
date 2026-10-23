"""Transcripción de notas de voz (SPEC-04, HU-MEDIA-03; ADR-0151).

Groq y OpenAI comparten el mismo endpoint multipart estilo Whisper. Sin SDK:
una llamada HTTP por mensaje. Devuelve texto vacío (`None`) cuando el audio no
tiene voz entendible; eso NO es un error de proveedor, es un resultado válido.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from core.config import get_settings
from core.media.catalog import AUDIO_CATALOG, audio_cost_usd
from core.media.errors import MediaProviderError, error_from_status

# OGG/Opus de 1 s de silencio (mismo audio de prueba de agent-sale): sirve para
# confirmar endpoint + llave + modelo sin necesitar una nota real.
PRUEBA_AUDIO_OGG = base64.b64decode(
    "T2dnUwACAAAAAAAAAACYmPdtAAAAAH5jkm0BE09wdXNIZWFkAQE4AYA+AAAAAABPZ2dTAAAAAAAAAAAAAJiY"
    "920BAAAAGl2c8gE+T3B1c1RhZ3MNAAAATGF2ZjYwLjE2LjEwMAEAAAAdAAAAZW5jb2Rlcj1MYXZjNjAuMzEu"
    "MTAyIGxpYm9wdXNPZ2dTAACAuwAAAAAAAJiY920CAAAAyZsFpTIDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMD"
    "AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA7j//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj"
    "//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj"
    "//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj"
    "//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//rj//k9nZ1MABLi8"
    "AAAAAAAAmJj3bQMAAADh7rhzAQO4//4="
)

_MIME_A_EXTENSION = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/amr": "amr",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/webm": "webm",
}


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str | None
    duration_s: float
    cost_usd: float
    provider: str
    model: str


def _extension(mime_type: str) -> str:
    base = (mime_type or "").split(";")[0].strip().lower()
    return _MIME_A_EXTENSION.get(base, "ogg")


def _duration(payload: dict[str, object]) -> float:
    raw = payload.get("duration")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        last = segments[-1]
        if isinstance(last, dict):
            end = last.get("end")
            if isinstance(end, (int, float)) and end > 0:
                return float(end)
    return 0.0


async def transcribir_audio(
    contenido: bytes,
    mime_type: str,
    *,
    provider: str,
    model: str,
    api_key: str,
    language: str = "es",
) -> TranscriptionResult:
    """Transcribe el audio. Lanza `MediaProviderError` solo si la API falla de verdad."""
    if provider not in AUDIO_CATALOG:
        raise MediaProviderError(f"Proveedor de audio desconocido: {provider}", code="audio_provider_invalido")
    entry = AUDIO_CATALOG[provider]  # type: ignore[index]
    if not api_key:
        raise MediaProviderError("Falta la llave del proveedor de audio.", code="audio_sin_llave")

    settings = get_settings()
    files = {
        "file": (f"audio.{_extension(mime_type)}", contenido, mime_type or "audio/ogg"),
    }
    # `verbose_json` devuelve `duration`, que usamos para el costo en sombra. Los
    # modelos gpt-4o-transcribe de OpenAI solo aceptan `json`/`text`.
    response_format = "verbose_json" if provider == "groq" or model == "whisper-1" else "json"
    data = {
        "model": model,
        "language": (language or "es")[:8],
        "response_format": response_format,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.stt_timeout_s) as client:
            response = await client.post(
                f"{entry.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
            )
    except httpx.TimeoutException as exc:
        raise MediaProviderError(
            "El proveedor de transcripción no respondió a tiempo.", transient=True, code="stt_timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaProviderError(
            f"No se pudo conectar con el proveedor de transcripción: {type(exc).__name__}",
            transient=True,
            code="stt_red",
        ) from exc

    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
            detail = str((body.get("error") or {}).get("message") or detail)
        except Exception:
            pass
        raise error_from_status(response.status_code, detail)

    try:
        payload = response.json()
    except Exception as exc:
        raise MediaProviderError(
            "El proveedor de transcripción devolvió una respuesta no-JSON.", transient=True, code="stt_respuesta"
        ) from exc
    if not isinstance(payload, dict):
        payload = {}

    text = payload.get("text")
    texto = str(text).strip() if isinstance(text, str) else ""
    duration_s = _duration(payload)
    return TranscriptionResult(
        text=texto or None,
        duration_s=duration_s,
        cost_usd=audio_cost_usd(provider, model, duration_s),  # type: ignore[arg-type]
        provider=provider,
        model=model,
    )


__all__ = ["PRUEBA_AUDIO_OGG", "TranscriptionResult", "transcribir_audio"]

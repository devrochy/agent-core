"""Síntesis de voz del pack (ADR-0197).

La llamada a OpenAI vive en el core (`core.media.tts`) para que el pack no
importe un cliente HTTP (ADR-0052). Acá quedan la llave, la normalización a
OGG/Opus y el guardado con el controlador único.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

from core.almacenamiento import URL_TTL_CANAL, StorageInvalido, guardar, url
from core.config import get_settings
from core.mcp.types import VoiceSynthesis
from core.media.errors import MediaProviderError
from core.media.tts import sintetizar_audio
from core.shared.logger import get_logger
from core.shared.tenant import current_tenant_slug

from .config import VoiceConfig
from .policy import PLUGIN_ID, cargar_config

log = get_logger(__name__)


def _a_ogg_opus(datos: bytes) -> bytes | None:
    """Normaliza la respuesta a OGG/Opus (WhatsApp PTT). `None` si no se pudo."""
    if datos.startswith(b"OggS"):
        return datos
    try:
        proceso = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-f",
                "ogg",
                "pipe:1",
            ],
            input=datos,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        log.warning("voice_tts_ffmpeg_failed", exc_info=True)
        return None
    if proceso.returncode != 0 or not proceso.stdout:
        log.warning("voice_tts_ffmpeg_error", stderr=proceso.stderr[:300].decode(errors="ignore"))
        return None
    return proceso.stdout


def _llave(config: VoiceConfig) -> str:
    propia = config.api_key.get_secret_value().strip()
    return propia or get_settings().openai_api_key


async def sintetizar(conversation_id, texto: str) -> VoiceSynthesis | None:  # type: ignore[no-untyped-def]
    """Sintetiza `texto` y lo deja guardado. `None` = el worker responde en texto."""
    config = await cargar_config()
    if config is None or not config.enabled or config.modo == "nunca" or not texto.strip():
        return None
    api_key = _llave(config)
    if not api_key:
        log.warning("voice_tts_sin_llave", provider=config.provider)
        return None

    try:
        resultado = await sintetizar_audio(
            texto,
            model=config.model,
            voice=config.voice,
            speed=config.speed,
            api_key=api_key,
        )
    except MediaProviderError as exc:
        log.warning("voice_tts_error", code=exc.code, transient=exc.transient)
        return None

    # OpenAI ya puede devolver Ogg/Opus; solo se invoca ffmpeg cuando hace falta.
    datos = resultado.contenido
    ogg = datos if datos.startswith(b"OggS") else await asyncio.to_thread(_a_ogg_opus, datos)
    if not ogg:
        return None

    slug = await current_tenant_slug()
    if not slug:
        log.warning("voice_tts_sin_slug", conversation_id=str(conversation_id))
        return None
    try:
        referencia = guardar(
            "media",
            ogg,
            mimetype="audio/ogg",
            tenant_slug=slug,
            subcarpeta="salientes",
            nombre_base=uuid.uuid4().hex,
        )
    except (StorageInvalido, OSError):
        log.warning("voice_tts_storage_failed", conversation_id=str(conversation_id), exc_info=True)
        return None

    log.info(
        "voice_tts_generada",
        conversation_id=str(conversation_id),
        bytes=len(ogg),
        plugin=PLUGIN_ID,
    )
    return VoiceSynthesis(
        audio_key=referencia.key,
        audio_url=url(referencia.key, ttl_s=URL_TTL_CANAL),
        mimetype="audio/ogg",
    )


__all__ = ["sintetizar"]

"""Descarga de media entrante de Telegram (ADR-0199, PR core 5).

El webhook trae el `file_id` de la foto o la nota de voz. Con el token del bot de la conexión de la conversación:
`getFile` da el `file_path` y su tamaño, y el archivo se baja de `https://api.telegram.org/file/bot<token>/<file_path>`
con el cliente del paquete, que solo habla con ese host, nunca registra el token y corta en el tope de bytes. Un bot
solo descarga archivos de hasta 20 MB; se usa el menor entre ese tope y `MEDIA_MAX_BYTES`. Mismo resultado
(`DownloadedMedia`) y misma clasificación de errores que Meta y Twilio: lo transitorio se reintenta, lo demás deja el
mensaje como «[Archivo adjunto]».
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

import httpx
from agentcore_channel_telegram.client import TelegramClient
from agentcore_channel_telegram.limites import ARCHIVO_DESCARGA_MAX

from core.config import get_settings
from core.gateway.channels.base import ProviderError
from core.media.errors import MediaProviderError
from core.media.meta_media import DownloadedMedia
from core.shared.logger import get_logger

log = get_logger(__name__)

_NO_RESPONDE = ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA")
# Telegram no informa el tipo: sale de lo que dijo el webhook o de la extensión del `file_path`.
_POR_EXTENSION = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                  ".oga": "audio/ogg", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def _error(exc: ProviderError) -> MediaProviderError:
    """`ProviderError` del cliente → `MediaProviderError`, sin URL ni token en el mensaje."""
    if exc.category in _NO_RESPONDE:
        return MediaProviderError(
            "Telegram no respondió al descargar el media.", transient=True, code="media_red"
        )
    if exc.category == "CONTENT":
        return MediaProviderError("El media de Telegram supera el tope.", code="media_demasiado_grande")
    if exc.category in ("AUTH", "CONFIG"):
        return MediaProviderError("Telegram rechazó la descarga del media.", code="media_sin_token")
    return MediaProviderError(
        f"Telegram rechazó la descarga del media (HTTP {exc.http_status}).", code="media_descarga"
    )


async def descargar_media_telegram(
    file_id: str,
    bot_token: str,
    *,
    mime_esperado: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> DownloadedMedia:
    """Descarga y valida un media de Telegram por su `file_id`. Lanza `MediaProviderError`."""
    if not bot_token:
        raise MediaProviderError(
            "No hay credenciales de la conexión para descargar la media.", code="media_sin_token"
        )
    if not file_id or len(file_id) > 256:
        raise MediaProviderError("El media de Telegram no trae un file_id válido.", code="media_sin_id")
    tope = min(get_settings().media_max_bytes, ARCHIVO_DESCARGA_MAX)
    try:
        cliente = TelegramClient(bot_token, transport=transport)
        archivo = await cliente.get_file(file_id) or {}
        tamano = archivo.get("file_size")
        if isinstance(tamano, int) and tamano > tope:
            raise MediaProviderError(
                f"El media pesa {tamano} bytes y el tope es {tope}.", code="media_demasiado_grande"
            )
        file_path = str(archivo.get("file_path") or "")
        if not file_path:
            raise MediaProviderError("Telegram no devolvió la ruta del archivo.", code="media_sin_id")
        datos = await cliente.download_file(file_path, max_bytes=tope)
    except ProviderError as exc:
        raise _error(exc) from None

    if not datos:
        raise MediaProviderError("Telegram devolvió un media vacío.", code="media_vacio")
    mime = mime_esperado.split(";")[0].strip().lower() or _POR_EXTENSION.get(
        PurePosixPath(file_path).suffix.lower(), ""
    )
    if not mime:
        raise MediaProviderError("No se sabe el tipo del media de Telegram.", code="media_mime")
    log.info("media_descargada", provider="telegram", mime_type=mime, bytes=len(datos))
    return DownloadedMedia(
        contenido=datos, mime_type=mime, sha256=hashlib.sha256(datos).hexdigest(), bytes=len(datos)
    )

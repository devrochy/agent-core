"""Media entrante de WhatsApp Web (waweb) ya descargada por el connector (ADR-0196).

El socket de Neonize vive en el proceso `connector`; el consumer corre en la API.
Los bytes cruzan por Redis en base64 bajo una clave por negocio y mensaje. Este
módulo valida que la referencia sea de ESTE negocio, decodifica y aplica el
mismo tope de tamaño que las descargas de Meta y Twilio.
"""

from __future__ import annotations

import base64
import hashlib
import uuid

from core.config import get_settings
from core.media.errors import MediaProviderError
from core.media.meta_media import DownloadedMedia
from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

_MEDIA_PREFIX = "waweb:media:"


async def descargar_media_waweb(
    item: dict[str, object], *, tenant_id: uuid.UUID
) -> DownloadedMedia:
    """Devuelve el media que el connector dejó en Redis. Lanza `MediaProviderError`."""
    mime_type = str(item.get("type") or "").split(";", 1)[0].strip().lower()
    clave = str(item.get("redis_key") or "")
    error = str(item.get("error") or "")
    if error or not clave or not mime_type:
        raise MediaProviderError(
            "El connector no pudo entregar el media de WhatsApp Web.",
            code="media_descarga",
        )

    prefijo = f"{_MEDIA_PREFIX}{tenant_id}:"
    if not clave.startswith(prefijo):
        raise MediaProviderError(
            "La referencia del media no pertenece a este negocio.",
            code="media_url_invalida",
        )
    try:
        crudo = await get_redis().get(clave)
    except Exception as exc:
        raise MediaProviderError(
            "No se pudo leer el media entrante de WhatsApp Web.",
            transient=True,
            code="media_red",
        ) from exc
    if not crudo:
        raise MediaProviderError(
            "El media entrante de WhatsApp Web ya expiró.",
            code="media_expirada",
        )

    try:
        datos = base64.b64decode(crudo, validate=True)
    except Exception as exc:
        raise MediaProviderError(
            "El media entrante de WhatsApp Web está corrupto.",
            code="media_corrupta",
        ) from exc
    if not datos:
        raise MediaProviderError("WhatsApp Web entregó un media vacío.", code="media_vacio")

    tope = get_settings().media_max_bytes
    if len(datos) > tope:
        raise MediaProviderError(
            f"El media supera el tope de {tope} bytes.",
            code="media_demasiado_grande",
        )

    log.info("media_descargada", provider="waweb", mime_type=mime_type, bytes=len(datos))
    return DownloadedMedia(
        contenido=datos,
        mime_type=mime_type,
        sha256=hashlib.sha256(datos).hexdigest(),
        bytes=len(datos),
    )


__all__ = ["descargar_media_waweb"]

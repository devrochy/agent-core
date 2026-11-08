"""Descarga de media entrante de WhatsApp vía Twilio (ADR-0186, PR-4).

El webhook trae `MediaUrlN` (`https://api.twilio.com/2010-04-01/Accounts/AC…/Messages/MM…/Media/ME…`), que exige
auth básica de la cuenta y responde con un redirect a la URL temporal del archivo. Se sigue UN solo salto, a mano:
la URL inicial tiene que ser de `api.twilio.com` y la del redirect, HTTPS y de un host permitido; las credenciales
no viajan al segundo host. Mismo tope de bytes, timeout y clasificación de errores que la descarga de Meta
(`meta_media.py`), y mismo resultado (`DownloadedMedia`).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

import httpx

from core.config import get_settings
from core.media.errors import MediaProviderError
from core.media.meta_media import DownloadedMedia
from core.shared.logger import get_logger

log = get_logger(__name__)

_HOST_API = "api.twilio.com"
# Destino del redirect de `MediaUrl`: el CDN de Twilio (por sufijo exacto) o un endpoint de S3 (no cualquier host
# de AWS: solo `s3.…amazonaws.com` / `s3-….amazonaws.com`).
_HOSTS_REDIRECT = (".twilio.com", ".twiliocdn.com")
_S3 = re.compile(r"^s3[.-][a-z0-9.-]*\.amazonaws\.com$")
# `MediaUrlN` de un mensaje de la cuenta: nada más se descarga con sus credenciales.
_RUTA_MEDIA = (
    r"^/2010-04-01/Accounts/{account}/Messages/(SM|MM)[0-9a-f]{{32}}/Media/ME[0-9a-f]{{32}}$"
)


def _es_api_de_twilio(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == _HOST_API


def _es_media_de_la_cuenta(url: str, account_sid: str) -> bool:
    if not _es_api_de_twilio(url) or not re.fullmatch(r"AC[0-9a-f]{32}", account_sid or ""):
        return False
    parsed = urlparse(url)
    return (
        not parsed.query
        and re.match(_RUTA_MEDIA.format(account=account_sid), parsed.path) is not None
    )


def _redirect_permitido(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return (
        host == _HOST_API
        or any(host.endswith(sufijo) for sufijo in _HOSTS_REDIRECT)
        or bool(_S3.match(host))
    )


def _error_http(status: int, code: str) -> MediaProviderError:
    return MediaProviderError(
        f"Twilio rechazó la descarga del media (HTTP {status}).",
        transient=status == 429 or status >= 500,
        code=code,
    )


async def _leer(response: httpx.Response, tope: int) -> tuple[bytes, str]:
    contenido = bytearray()
    async for chunk in response.aiter_bytes():
        contenido.extend(chunk)
        if len(contenido) > tope:
            raise MediaProviderError(
                f"El media supera el tope de {tope} bytes.", code="media_demasiado_grande"
            )
    return bytes(contenido), response.headers.get("content-type", "")


async def descargar_media_twilio(
    url: str,
    account_sid: str,
    auth_token: str,
    *,
    mime_esperado: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> DownloadedMedia:
    """Descarga y valida un media de Twilio. Lanza `MediaProviderError`."""
    if not account_sid or not auth_token:
        raise MediaProviderError(
            "No hay credenciales de la conexión para descargar la media.", code="media_sin_token"
        )
    if not url or not _es_media_de_la_cuenta(url, account_sid):
        raise MediaProviderError(
            "La URL del media no es de un mensaje de esta cuenta de Twilio.",
            code="media_url_invalida",
        )

    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=settings.media_download_timeout_s, follow_redirects=False, transport=transport
        ) as client:
            async with client.stream("GET", url, auth=(account_sid, auth_token)) as primera:
                if not primera.is_redirect:
                    if primera.status_code >= 400:
                        raise _error_http(primera.status_code, "media_descarga")
                    datos, mime_type = await _leer(primera, settings.media_max_bytes)
                    destino = ""
                else:
                    destino = urljoin(url, primera.headers.get("location", ""))
            if destino:
                if not _redirect_permitido(destino):
                    raise MediaProviderError(
                        "Twilio redirigió el media a un host no permitido.",
                        code="media_url_invalida",
                    )
                # Las credenciales de la cuenta no salen de api.twilio.com.
                auth = (account_sid, auth_token) if _es_api_de_twilio(destino) else None
                async with client.stream("GET", destino, auth=auth) as response:
                    if response.is_redirect:
                        raise MediaProviderError(
                            "Twilio redirigió el media más de una vez.", code="media_url_invalida"
                        )
                    if response.status_code >= 400:
                        raise _error_http(response.status_code, "media_descarga")
                    datos, mime_type = await _leer(response, settings.media_max_bytes)
    except httpx.TimeoutException as exc:
        raise MediaProviderError(
            "La descarga del media de Twilio no terminó a tiempo.",
            transient=True,
            code="media_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaProviderError(
            f"No se pudo conectar con Twilio para descargar el media: {type(exc).__name__}",
            transient=True,
            code="media_red",
        ) from exc

    if not datos:
        raise MediaProviderError("Twilio devolvió un media vacío.", code="media_vacio")
    mime = (mime_type or mime_esperado).split(";")[0].strip().lower()
    if not mime:
        raise MediaProviderError("Twilio no informó el tipo del media.", code="media_mime")
    log.info("media_descargada", provider="twilio", mime_type=mime, bytes=len(datos))
    return DownloadedMedia(
        contenido=datos, mime_type=mime, sha256=hashlib.sha256(datos).hexdigest(), bytes=len(datos)
    )

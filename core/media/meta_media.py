"""Descarga de media entrante de Meta Cloud API (SPEC-04, HU-MEDIA-02; ADR-0151).

Dos pasos, como exige Meta: `GET /{media_id}` devuelve una URL temporal y luego
se descargan los bytes de esa URL con el mismo Bearer token. La URL no es
pública aunque lo parezca.

Defensas: solo HTTPS y hosts de Meta/`fbcdn` (evita SSRF), tope de bytes y
timeout. Cualquier falla se clasifica como transitoria o permanente.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from core.config import get_settings
from core.media.errors import MediaProviderError
from core.shared.logger import get_logger

log = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
# Hosts válidos de la URL temporal de Meta. Se compara por sufijo exacto.
_HOSTS_PERMITIDOS = (
    ".fbsbx.com",
    ".facebook.com",
    ".fbcdn.net",
)


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    contenido: bytes
    mime_type: str
    sha256: str
    bytes: int


def _host_permitido(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == sufijo[1:] or host.endswith(sufijo) for sufijo in _HOSTS_PERMITIDOS)


def _graph_url(media_id: str) -> str:
    version = get_settings().graph_api_version
    return f"{GRAPH_BASE}/{version}/{media_id}"


async def descargar_media(media_id: str, access_token: str) -> DownloadedMedia:
    """Descarga y valida un media de Meta. Lanza `MediaProviderError`."""
    if not media_id:
        raise MediaProviderError("El mensaje no trae id de media.", code="media_sin_id")
    if not access_token:
        raise MediaProviderError(
            "No hay token de la conexión para descargar la media.", code="media_sin_token"
        )

    settings = get_settings()
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=settings.media_download_timeout_s) as client:
            meta_response = await client.get(_graph_url(media_id), headers=headers)
            if meta_response.status_code >= 400:
                raise MediaProviderError(
                    f"Meta rechazó la consulta del media (HTTP {meta_response.status_code}).",
                    transient=meta_response.status_code == 429 or meta_response.status_code >= 500,
                    code="media_metadata",
                )
            try:
                metadata = meta_response.json()
            except Exception as exc:
                raise MediaProviderError(
                    "Meta devolvió una respuesta no-JSON para el media.",
                    transient=True,
                    code="media_metadata_respuesta",
                ) from exc
            url = str(metadata.get("url") or "")
            mime_type = str(metadata.get("mime_type") or "").split(";")[0].strip()
            file_size = metadata.get("file_size")
            sha_esperado = str(metadata.get("sha256") or "")
            if not url or not _host_permitido(url):
                raise MediaProviderError(
                    "Meta devolvió una URL de media no permitida.", code="media_url_invalida"
                )
            if isinstance(file_size, (int, float)) and file_size > settings.media_max_bytes:
                raise MediaProviderError(
                    f"El media pesa {int(file_size)} bytes y el tope es {settings.media_max_bytes}.",
                    code="media_demasiado_grande",
                )

            contenido = bytearray()
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code >= 400:
                    raise MediaProviderError(
                        f"No se pudo descargar el media de Meta (HTTP {response.status_code}).",
                        transient=response.status_code == 429 or response.status_code >= 500,
                        code="media_descarga",
                    )
                async for chunk in response.aiter_bytes():
                    contenido.extend(chunk)
                    if len(contenido) > settings.media_max_bytes:
                        raise MediaProviderError(
                            f"El media supera el tope de {settings.media_max_bytes} bytes.",
                            code="media_demasiado_grande",
                        )
    except httpx.TimeoutException as exc:
        raise MediaProviderError(
            "La descarga del media de Meta no terminó a tiempo.", transient=True, code="media_timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaProviderError(
            f"No se pudo conectar con Meta para descargar el media: {type(exc).__name__}",
            transient=True,
            code="media_red",
        ) from exc

    datos = bytes(contenido)
    if not datos:
        raise MediaProviderError("Meta devolvió un media vacío.", code="media_vacio")
    sha = hashlib.sha256(datos).hexdigest()
    if sha_esperado and sha_esperado.lower() != sha:
        raise MediaProviderError(
            "El hash del media descargado no coincide con el de Meta.", code="media_hash"
        )
    if not mime_type:
        raise MediaProviderError("Meta no informó el tipo del media.", code="media_mime")
    log.info(
        "media_descargada",
        media_id=media_id,
        mime_type=mime_type,
        bytes=len(datos),
    )
    return DownloadedMedia(contenido=datos, mime_type=mime_type, sha256=sha, bytes=len(datos))


__all__ = ["DownloadedMedia", "descargar_media"]

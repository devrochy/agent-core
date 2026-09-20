"""Resumable Upload API de Meta, usada para subir la cabecera multimedia de
una plantilla ANTES de crearla.

IMPORTANTE -- `header_handle` (el `"h"` que devuelve el paso 2 de acá) NO ES
`media_id`: el handle sirve SOLO para crear/editar la plantilla
(`example.header_handle`); el `media_id` de `POST /{phone_number_id}/media`
es un objeto completamente distinto, usado para *enviar* un mensaje con esa
cabecera. No son intercambiables.

Paso 1: `POST /{APP_ID}/uploads?file_name=&file_length=&file_type=` con un
APP access token (no el token de sistema de la WABA) -- pasa por
`graph_fetch` normal, mismo host/versión, solo query params. Devuelve
`{"id": "upload:<SESSION_ID>"}`, el prefijo "upload:" ya viene incluido.

Paso 2: `POST /{upload_id}` (el id completo del paso 1, con su prefijo) con
`Authorization: OAuth <token>` (NO "Bearer") y header `file_offset: 0`,
cuerpo binario crudo -- por eso NO pasa por `graph_fetch` (que siempre pone
Bearer + appsecret_proof + serializa `body` como JSON). Devuelve
`{"h": "<HANDLE>"}`.
"""

from __future__ import annotations

import uuid

import httpx

from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import GRAPH_BASE, graph_fetch
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 60.0

ALLOWED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/jpg", "image/png", "video/mp4"}


class TemplateUploadError(AgentCoreError):
    """Error subiendo la cabecera multimedia -- nunca se llega a crear la plantilla."""


def validate_mime_type(file_type: str) -> None:
    if file_type not in ALLOWED_MIME_TYPES:
        raise TemplateUploadError(f"tipo de archivo no admitido para cabecera de plantilla: {file_type!r}")


async def start_upload_session(
    app_id: str, app_access_token: str, app_secret: str, version: str, connection_id: uuid.UUID,
    *, file_name: str, file_length: int, file_type: str,
) -> str:
    """Devuelve el `id` completo tal cual lo manda Meta (con el prefijo
    `"upload:"` ya incluido) -- no se reconstruye a mano."""
    validate_mime_type(file_type)
    data = await graph_fetch(
        f"/{app_id}/uploads", method="POST", token=app_access_token, app_secret=app_secret,
        query={"file_name": file_name, "file_length": str(file_length), "file_type": file_type},
        version=version, connection_id=connection_id,
    )
    upload_id = str((data or {}).get("id") or "")
    if not upload_id:
        raise TemplateUploadError("Meta no devolvió un id de sesión de upload")
    return upload_id


async def upload_file_chunk(upload_id: str, access_token: str, file_bytes: bytes, *, file_offset: int = 0) -> str:
    """`upload_id` ya trae el prefijo `"upload:"` -- la URL NO lleva el
    prefijo de versión de Graph (`/v21.0/...`), es `{GRAPH_BASE}/{upload_id}`
    directo."""
    url = f"{GRAPH_BASE}/{upload_id}"
    headers = {"Authorization": f"OAuth {access_token}", "file_offset": str(file_offset)}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(url, headers=headers, content=file_bytes)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise GraphError(f"No se pudo subir el archivo a Meta: {exc}", category="TRANSIENT", is_transient=True) from exc

    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = None
        raise classify_and_build_error(
            http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text
        )

    handle = str((response.json() or {}).get("h") or "")
    if not handle:
        raise TemplateUploadError("Meta no devolvió un handle ('h') tras el upload")
    return handle

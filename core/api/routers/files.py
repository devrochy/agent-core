"""Servidor de archivos firmados (`GET /api/v1/files/{token}`, ADR-0085 enmendado).

El token lleva la `key` y su expiración, firmado con `APP_SECRET_KEY`; es la única
autorización. No se expone la ruta interna ni se lista nada.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter
from fastapi.responses import FileResponse

from core.almacenamiento import abrir, firmas
from core.api.errors import ApiError

router = APIRouter(prefix="/files", tags=["archivos"])


@router.get("/{token}")
async def serve_file(token: str) -> FileResponse:
    key = firmas.verificar(token)
    if key is None:
        raise ApiError.not_found("El enlace del archivo no es válido o venció.")
    ruta = abrir(key)
    if ruta is None:
        raise ApiError.not_found("El archivo no existe.")
    media = mimetypes.guess_type(ruta.name)[0] or "application/octet-stream"
    return FileResponse(
        ruta,
        media_type=media,
        headers={"Cache-Control": "private, max-age=300"},
    )

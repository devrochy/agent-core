"""Errores de la API `/api/v1` (ADR-0077).

Un solo formato de error para todos los realms:

    {"error": {"code": "<slug>", "message": "<texto para el usuario>"}}

Opcionales: `fields` (`{campo: mensaje}`, HU-FWK-08) y `details` (datos para que el
cliente muestre el estado real, p. ej. `used`/`limit` de una cuota).

Se lanza `ApiError` desde cualquier endpoint o dependencia; el handler se
registra en `core.main` y solo aplica a rutas de la API. Los errores de validación del
cuerpo (Pydantic) bajo `/api/v1` responden 422 con el MISMO sobre
(`validation_error_handler`): el panel y la app leen `error.message` y `error.fields`,
no el `detail` crudo de FastAPI.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from core.shared.validacion import MENSAJE_CAMPOS, campos_de

API_PREFIX = "/api/"


class ApiError(Exception):
    """Error de dominio de la API con código y status HTTP estables."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        fields: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        # Errores por campo (HU-FWK-08): `{campo: mensaje}` para que el formulario
        # los muestre junto al control.
        self.fields = fields
        # Estado real para el cliente (p. ej. la cuota: `used`, `limit`, `enabled`).
        self.details = details

    @classmethod
    def unauthorized(cls, message: str = "Sesión requerida.") -> ApiError:
        return cls(401, "unauthorized", message)

    @classmethod
    def forbidden(cls, message: str = "Sin permiso.") -> ApiError:
        return cls(403, "forbidden", message)

    @classmethod
    def not_found(cls, message: str = "No encontrado.") -> ApiError:
        return cls(404, "not_found", message)

    @classmethod
    def bad_request(
        cls, message: str, code: str = "bad_request", fields: dict[str, str] | None = None
    ) -> ApiError:
        return cls(400, code, message, fields=fields)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Serializa `ApiError` al sobre JSON de la API."""
    error: dict = {"code": exc.code, "message": exc.message}
    if exc.fields:
        error["fields"] = exc.fields
    if exc.details:
        error["details"] = exc.details
    return JSONResponse({"error": error}, status_code=exc.status_code)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    """422 con el sobre de la API para las rutas de `/api/v1` (también `/api/v1/pack/`).

    `fields` trae cada campo con su mensaje en español (un solo formato de error, ADR-0077;
    el panel y la APK leen `error.message`, ninguno leía el `detail` de FastAPI). Fuera de la
    API (panel SSR, webhooks) sigue el handler por defecto."""
    if not request.url.path.startswith(API_PREFIX):
        return await request_validation_exception_handler(request, exc)
    error: dict[str, Any] = {
        "code": "validation_error",
        "message": MENSAJE_CAMPOS,
        "fields": campos_de(exc.errors()),
    }
    return JSONResponse({"error": error}, status_code=422)

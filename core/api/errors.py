"""Errores de la API `/api/v1` (ADR-0077).

Un solo formato de error para todos los realms:

    {"error": {"code": "<slug>", "message": "<texto para el usuario>"}}

Se lanza `ApiError` desde cualquier endpoint o dependencia; el handler se
registra en `core.main` y solo aplica a rutas de la API.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Error de dominio de la API con código y status HTTP estables."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

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
    def bad_request(cls, message: str, code: str = "bad_request") -> ApiError:
        return cls(400, code, message)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Serializa `ApiError` al sobre JSON de la API."""
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=exc.status_code,
    )

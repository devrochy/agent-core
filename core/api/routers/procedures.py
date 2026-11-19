"""Procedimientos (`procedure/1`, ADR-0113): validador y linter para autores y el master.

- `POST /api/v1/admin/procedures/validate` — `{"procedure": {...}}` → `{valid, errors}`
  con un error por problema (forma del contrato y linter). Responde 200 también cuando
  hay errores: el panel los muestra todos juntos.

No lee ni escribe datos de ningún negocio: valida contra las acciones del binario (core
y habilidades cargadas, `plugin_manager.list_modules()`), que son las mismas para todos.
Nunca incluye las tools declarativas de un negocio. Requiere escritura en la sección
`plugins` (Habilidades), porque es la herramienta de quien escribe procedimientos.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.kernel.procedures import MAX_PROCEDURE_BYTES, catalogo_de_acciones, revisar
from core.plugin_manager import plugin_manager

router = APIRouter(prefix="/admin/procedures", tags=["admin · procedimientos"])

# El sobre `{"procedure": ...}` suma unos bytes al procedimiento.
_MAX_BODY_BYTES = MAX_PROCEDURE_BYTES + 1024

_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["procedure"],
                "properties": {
                    "procedure": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Procedimiento procedure/1 (ADR-0113).",
                    }
                },
                "additionalProperties": False,
            }
        }
    },
}


class ProcedureIssue(BaseModel):
    code: str
    path: str
    message: str


class ProcedureValidateOut(BaseModel):
    valid: bool
    errors: list[ProcedureIssue]


def _too_large() -> ApiError:
    return ApiError(
        413, "payload_too_large", f"El procedimiento pesa más de {MAX_PROCEDURE_BYTES // 1024} KiB."
    )


async def _read_body(request: Request) -> bytes:
    """Cuerpo con tope: se corta antes de leer de más (no confía en Content-Length)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise _too_large()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise _too_large()
    return bytes(body)


@router.post(
    "/validate",
    response_model=ProcedureValidateOut,
    openapi_extra={"requestBody": _REQUEST_BODY},
)
async def validate_procedure(
    request: Request,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> ProcedureValidateOut:
    """Valida un procedimiento `procedure/1` y aplica el linter."""
    body = await _read_body(request)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        # ValueError cubre JSONDecodeError, UnicodeDecodeError y el tope de dígitos de un
        # entero; RecursionError, un JSON anidado a propósito (`[[[[...]]]]`).
        raise ApiError.bad_request("El cuerpo no es JSON válido.", code="invalid_json") from None
    if not isinstance(payload, dict) or set(payload) != {"procedure"}:
        raise ApiError.bad_request(
            'Se espera {"procedure": {...}}.', code="invalid_body", fields={"procedure": "Obligatorio."}
        )
    issues = revisar(payload["procedure"], catalogo_de_acciones(plugin_manager.list_modules()))
    return ProcedureValidateOut(
        valid=not issues,
        errors=[ProcedureIssue(**issue.as_dict()) for issue in issues],
    )

"""Editor de tools declarativas en `/api/v1` (ADR-0052, ADR-0113 §2, HU-AOS-12).

Hasta ahora solo existía en el panel SSR. Edita `extensions.tools` del AgentSpec v2 activo
(lo que lee el loop legacy) con los headers sensibles por referencia:

- `GET  /api/v1/admin/settings/declarative-tools` — las tools (los literales sensibles que
  queden de specs viejos salen enmascarados, nunca el valor), los secretos configurados (solo
  nombres y dominios) y qué literales faltan por migrar.
- `PUT  /api/v1/admin/settings/declarative-tools` — `{tools, secrets}`. `secrets` es
  `{nombre: valor | null}` de solo escritura (`null` lo quita). Un header sensible con un valor
  literal se rechaza (422); con la máscara, el literal guardado se migra a un secreto. Cada
  secreto queda atado a los dominios de las tools que lo usan. Los secretos se guardan cifrados
  en la instalación `core` ANTES que el spec: el loop nunca ve una referencia sin su secreto.

Permisos: sección `settings` (`write` para el PUT), como el resto de la Configuración.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from core.agents.declarativas import enmascarar, literales_sin_migrar, preparar
from core.agents.secretos import (
    SecretoInvalido,
    guardar_secretos_declarativos,
    secretos_configurados,
)
from core.agents.spec import load_active_spec, save_spec_if_changed
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.kernel.procedures import catalogo_de_acciones
from core.plugin_manager import plugin_manager

router = APIRouter(prefix="/admin/settings/declarative-tools", tags=["admin · configuración"])

_MAX_BODY_BYTES = 256 * 1024

_PUT_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["tools"],
                "properties": {
                    "tools": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                        "description": "Tools declarativas (ADR-0052). Headers sensibles como {\"secret\": \"<nombre>\"}.",
                    },
                    "secrets": {
                        "type": "object",
                        "additionalProperties": {"type": ["string", "null"]},
                        "description": "Valores de secretos (solo escritura); null quita el secreto.",
                    },
                },
                "additionalProperties": False,
            }
        }
    },
}


class ConfiguredSecret(BaseModel):
    name: str
    domains: list[str]


class DeclarativeToolsOut(BaseModel):
    tools: list[dict[str, Any]]
    secrets: list[ConfiguredSecret]
    literals_to_migrate: list[str]


async def _salida(tenant_id: Any) -> DeclarativeToolsOut:
    spec = await load_active_spec()
    raw = (spec.extensions or {}).get("tools")
    configurados = await secretos_configurados(tenant_id)
    return DeclarativeToolsOut(
        tools=enmascarar(raw),
        secrets=[ConfiguredSecret(name=n, domains=d) for n, d in sorted(configurados.items())],
        literals_to_migrate=literales_sin_migrar(raw),
    )


def _no_finito(valor: str) -> Any:
    raise ValueError(f"número no finito: {valor}")


async def _leer_json(request: Request) -> Any:
    declarado = request.headers.get("content-length")
    if declarado is not None and declarado.isascii() and declarado.isdigit() and int(declarado) > _MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "El cuerpo es demasiado grande.")
    cuerpo = bytearray()
    async for chunk in request.stream():
        cuerpo.extend(chunk)
        if len(cuerpo) > _MAX_BODY_BYTES:
            raise ApiError(413, "payload_too_large", "El cuerpo es demasiado grande.")
    try:
        return json.loads(bytes(cuerpo), parse_constant=_no_finito)
    except (ValueError, RecursionError):
        raise ApiError.bad_request("El cuerpo no es JSON válido.", code="invalid_json") from None


def _tenant(principal: Principal) -> Any:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


@router.get("", response_model=DeclarativeToolsOut)
async def get_declarative_tools(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> DeclarativeToolsOut:
    return await _salida(_tenant(principal))


@router.put("", response_model=DeclarativeToolsOut, openapi_extra={"requestBody": _PUT_BODY})
async def save_declarative_tools(
    request: Request,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> DeclarativeToolsOut:
    tid = _tenant(principal)
    cuerpo = await _leer_json(request)
    secretos = cuerpo.get("secrets", {}) if isinstance(cuerpo, dict) else None
    if (
        not isinstance(cuerpo, dict)
        or not set(cuerpo) <= {"tools", "secrets"}
        or "tools" not in cuerpo
        or not isinstance(secretos, dict)
        or not all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in secretos.items())
    ):
        raise ApiError.bad_request(
            'Se espera {"tools": [...], "secrets": {"<nombre>": "<valor>" | null}}.', code="invalid_body"
        )
    spec = await load_active_spec()
    actuales = (spec.extensions or {}).get("tools")
    configurados = await secretos_configurados(tid)
    acciones = set(catalogo_de_acciones(plugin_manager.list_modules()))
    preparadas = preparar(cuerpo["tools"], actuales, secretos, configurados, acciones)
    if preparadas.errores:
        raise ApiError(
            422,
            "invalid_declarative_tools",
            f"Hay {len(preparadas.errores)} problema(s) en las tools declarativas.",
            fields=preparadas.errores,
        )
    if preparadas.secretos or preparadas.quitar:
        try:
            await guardar_secretos_declarativos(
                tid, preparadas.secretos, quitar=preparadas.quitar, actor=principal.admin.id
            )
        except SecretoInvalido as exc:
            raise ApiError(422, "invalid_secret", str(exc)) from None
    data = spec.model_dump(mode="json")
    data["extensions"] = dict(data.get("extensions") or {}) | {"tools": preparadas.tools}
    await save_spec_if_changed(spec, data, tid)
    return await _salida(tid)

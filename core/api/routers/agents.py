"""Agentes del negocio con borrador y versiones inmutables (ADR-0113, ADR-0077, HU-AOS-12).

- `GET  /api/v1/admin/agents` — agentes con su última versión.
- `POST /api/v1/admin/agents` — `{key, kind, name, description}` → agente + borrador.
- `GET/PATCH /api/v1/admin/agents/{id}` — detalle (con borrador y versiones) / nombre,
  descripción y estado (`key` y `kind` no cambian).
- `GET/PUT /api/v1/admin/agents/{id}/draft` — `{content, lock_version}`: 409 si otro lo
  cambió (`lock_version` viejo), 422 con los errores del contrato (el mismo validador del CLI
  y de CI) si el spec es inválido.
- `GET/POST /api/v1/admin/agents/{id}/versions` — historial / publicar el borrador en una
  versión inmutable con `content_hash` (no la activa: la release llega con HU-AOS-14).
- `GET /api/v1/admin/agents/{id}/versions/{vid}` — contenido congelado.

Permisos: sección `settings` (lectura; `write` para escribir). Mientras el runtime del negocio
sea `legacy` (ADR-0112) un agente no hace nada, así que las escrituras quedan para el master o
para los negocios con `aos_runtime = 'v3'` (403 `aos_runtime_legacy` si no). Las keys del lector
tolerante (`general`, `coordinador`) están reservadas para el backfill de HU-AOS-13.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from core.agents import versiones as svc
from core.agents.contrato_v3 import MAX_SPEC_BYTES
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.kernel.procedures import Issue, catalogo_de_acciones
from core.plugin_manager import plugin_manager

router = APIRouter(prefix="/admin/agents", tags=["admin · agentes"])

_MAX_BODY_BYTES = MAX_SPEC_BYTES + 1024
Kind = Literal["core", "specialist", "backoffice"]


# ------------------------------------------------------------------ esquemas
class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    kind: Kind
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class AgentPatch(BaseModel):
    # `key` y `kind` no cambian: la instancia fija su rol por FK.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    status: Literal["active", "archived"] | None = None


class AgentSummary(BaseModel):
    id: uuid.UUID
    key: str
    kind: Kind
    contract: str
    name: str
    description: str
    status: str
    latest_version: int | None
    created_at: datetime


class AgentDraft(BaseModel):
    content: dict[str, Any]
    lock_version: int
    base_version_id: uuid.UUID | None
    updated_at: datetime


class AgentVersionSummary(BaseModel):
    id: uuid.UUID
    version: int
    label: str
    contract: str
    content_hash: str
    notes: str
    legacy: bool
    created_at: datetime


class AgentVersionDetail(AgentVersionSummary):
    content: dict[str, Any]


class AgentDetail(AgentSummary):
    draft: AgentDraft
    versions: list[AgentVersionSummary]


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Opcional: si se envía, publica solo si el borrador sigue en esa versión (409 si no).
    lock_version: int | None = Field(default=None, ge=0, lt=2**31)
    label: str | None = Field(default=None, max_length=60)
    notes: str = Field(default="", max_length=2000)


class PublishOut(BaseModel):
    version: AgentVersionSummary
    created: bool


_DRAFT_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["content", "lock_version"],
                "properties": {
                    "content": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "AgentSpec v3 (agentspec/3) o KernelSpec v1 (kernelspec/1), según el kind.",
                    },
                    "lock_version": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            }
        }
    },
}


# ------------------------------------------------------------------ utilidades
def _summary(agente: Any, ultima: int | None) -> AgentSummary:
    return AgentSummary(
        id=agente.id,
        key=agente.key,
        kind=agente.kind,
        contract=svc.CONTRATO_POR_KIND[agente.kind],
        name=agente.name,
        description=agente.description,
        status=agente.status,
        latest_version=ultima,
        created_at=agente.created_at,
    )


def _version(v: Any) -> AgentVersionSummary:
    return AgentVersionSummary(
        id=v.id,
        version=v.version,
        label=v.label,
        contract=v.contract,
        content_hash=v.content_hash,
        notes=v.notes,
        legacy=v.legacy_agent_spec_id is not None,
        created_at=v.created_at,
    )


def _draft(d: Any) -> AgentDraft:
    return AgentDraft(
        content=d.content, lock_version=d.lock_version, base_version_id=d.base_version_id, updated_at=d.updated_at
    )


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _puede_escribir(principal: Principal) -> uuid.UUID:
    tid = _tenant(principal)
    if principal.admin.role != "master" and not await svc.escritura_habilitada(tid):
        raise ApiError(
            403,
            "aos_runtime_legacy",
            "Los agentes se activan con el Agent OS de este negocio: por ahora solo los edita el administrador.",
        )
    return tid


def _contrato_422(issues: list[Issue]) -> ApiError:
    campos: dict[str, str] = {}
    for issue in issues:
        campos[issue.path] = f"{campos[issue.path]} {issue.message}" if issue.path in campos else issue.message
    return ApiError(422, "invalid_spec", f"El spec tiene {len(issues)} problema(s) del contrato.", fields=campos)


def _no_finito(valor: str) -> Any:
    raise ValueError(f"número no finito: {valor}")


def _float_finito(valor: str) -> float:
    numero = float(valor)
    if not math.isfinite(numero):  # `1e999` también es Infinity
        raise ValueError("número no finito")
    return numero


async def _leer_json(request: Request) -> Any:
    """Cuerpo con tope (no confía en Content-Length), sin NaN/Infinity ni anidación abusiva."""
    declarado = request.headers.get("content-length")
    if declarado is not None and declarado.isascii() and declarado.isdigit() and int(declarado) > _MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", f"El cuerpo pesa más de {_MAX_BODY_BYTES // 1024} KiB.")
    cuerpo = bytearray()
    async for chunk in request.stream():
        cuerpo.extend(chunk)
        if len(cuerpo) > _MAX_BODY_BYTES:
            raise ApiError(413, "payload_too_large", f"El cuerpo pesa más de {_MAX_BODY_BYTES // 1024} KiB.")
    try:
        return json.loads(bytes(cuerpo), parse_constant=_no_finito, parse_float=_float_finito)
    except (ValueError, RecursionError):
        raise ApiError.bad_request("El cuerpo no es JSON válido.", code="invalid_json") from None


def _catalogo() -> Any:
    return catalogo_de_acciones(plugin_manager.list_modules())


# ------------------------------------------------------------------ rutas
@router.get("", response_model=list[AgentSummary])
async def list_agents(principal: Annotated[Principal, Depends(require("settings"))]) -> list[AgentSummary]:
    return [_summary(a, v) for a, v in await svc.listar_agentes(_tenant(principal))]


@router.post("", response_model=AgentSummary, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> AgentSummary:
    tid = await _puede_escribir(principal)
    try:
        agente = await svc.crear_agente(
            tid,
            key=payload.key,
            kind=payload.kind,
            name=payload.name,
            description=payload.description,
            actor=principal.admin.id,
        )
    except svc.Conflicto as exc:
        raise ApiError(409, "conflict", str(exc), fields={"key": str(exc)}) from None
    return _summary(agente, None)


@router.get("/{agent_id}", response_model=AgentDetail)
async def get_agent(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("settings"))]
) -> AgentDetail:
    tid = _tenant(principal)
    try:
        agente = await svc.obtener_agente(tid, agent_id)
        borrador = await svc.obtener_borrador(tid, agent_id)
        versiones = await svc.listar_versiones(tid, agent_id)
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    base = _summary(agente, versiones[0].version if versiones else None)
    return AgentDetail(**base.model_dump(), draft=_draft(borrador), versions=[_version(v) for v in versiones])


@router.patch("/{agent_id}", response_model=AgentSummary)
async def patch_agent(
    agent_id: uuid.UUID,
    payload: AgentPatch,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> AgentSummary:
    tid = await _puede_escribir(principal)
    try:
        agente = await svc.actualizar_agente(
            tid, agent_id, name=payload.name, description=payload.description, status=payload.status
        )
        versiones = await svc.listar_versiones(tid, agent_id)
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    except svc.Conflicto as exc:
        raise ApiError(409, "conflict", str(exc)) from None
    return _summary(agente, versiones[0].version if versiones else None)


@router.get("/{agent_id}/draft", response_model=AgentDraft)
async def get_draft(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("settings"))]
) -> AgentDraft:
    try:
        return _draft(await svc.obtener_borrador(_tenant(principal), agent_id))
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None


@router.put("/{agent_id}/draft", response_model=AgentDraft, openapi_extra={"requestBody": _DRAFT_BODY})
async def save_draft(
    agent_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> AgentDraft:
    tid = await _puede_escribir(principal)
    cuerpo = await _leer_json(request)
    lock = cuerpo.get("lock_version") if isinstance(cuerpo, dict) else None
    if (
        not isinstance(cuerpo, dict)
        or set(cuerpo) != {"content", "lock_version"}
        or not isinstance(lock, int)
        or isinstance(lock, bool)
        or lock < 0
        or lock >= 2**31
    ):
        raise ApiError.bad_request(
            'Se espera {"content": {...}, "lock_version": <entero>}.',
            code="invalid_body",
            fields={"lock_version": "Obligatorio."},
        )
    try:
        borrador = await svc.guardar_borrador(
            tid, agent_id, cuerpo["content"], lock, catalogo=_catalogo(), actor=principal.admin.id
        )
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    except svc.ErroresDeContrato as exc:
        raise _contrato_422(exc.issues) from None
    except svc.Conflicto as exc:
        raise ApiError(409, "stale_lock_version", str(exc), fields={"lock_version": str(exc.lock_version)}) from None
    return _draft(borrador)


@router.get("/{agent_id}/versions", response_model=list[AgentVersionSummary])
async def list_versions(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("settings"))]
) -> list[AgentVersionSummary]:
    try:
        return [_version(v) for v in await svc.listar_versiones(_tenant(principal), agent_id)]
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None


@router.post("/{agent_id}/versions", response_model=PublishOut)
async def publish_version(
    agent_id: uuid.UUID,
    payload: PublishIn,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> PublishOut:
    tid = await _puede_escribir(principal)
    try:
        version, creada = await svc.publicar(
            tid,
            agent_id,
            catalogo=_catalogo(),
            actor=principal.admin.id,
            notes=payload.notes,
            label=payload.label,
            lock_version=payload.lock_version,
        )
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    except svc.ErroresDeContrato as exc:
        raise _contrato_422(exc.issues) from None
    except svc.Conflicto as exc:
        raise ApiError(409, "conflict", str(exc)) from None
    return PublishOut(version=_version(version), created=creada)


@router.get("/{agent_id}/versions/{version_id}", response_model=AgentVersionDetail)
async def get_version(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("settings"))],
) -> AgentVersionDetail:
    try:
        v = await svc.obtener_version(_tenant(principal), agent_id, version_id)
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Versión no encontrada.") from None
    return AgentVersionDetail(**_version(v).model_dump(), content=v.content)

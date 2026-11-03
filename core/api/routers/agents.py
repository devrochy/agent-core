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
- `GET /api/v1/admin/agents/{id}/versions/{vid}` — contenido congelado y, para un AgentSpec v3,
  sus `procedures` resueltos (HU-AOS-16) contra lo que publican los packs habilitados del
  negocio con este binario: `{id, range, resolved_version | null, problem | null}`. Lo que
  congela una release puede diferir (otra versión del pack o instalaciones por conexión).

- `GET /api/v1/admin/agents/retired-references` y `GET /api/v1/admin/agents/{id}/retired-references` —
  aviso en palabras simples de lo que el agente nombra y ya no existe (HU-AOS-59, ADR-0184). Solo lectura.

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

from core.admin.roles import actua_como_master
from core.agents import versiones as svc
from core.agents.aviso_retiros import CODIGO_AVISO, problemas_de_publicacion
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


class ResolvedProcedure(BaseModel):
    id: str
    range: str
    resolved_version: str | None
    problem: str | None


class AgentVersionDetail(AgentVersionSummary):
    content: dict[str, Any]
    procedures: list[ResolvedProcedure] = []


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


# ------------------------------------------------------------------ referencias retiradas (HU-AOS-59)
class RetiredNotice(BaseModel):
    """Una cosa que el agente nombra y ya no existe, con el texto para el dueño (sin códigos internos)."""

    scope: Literal["draft", "published", "in_use"] = Field(
        description="`draft`: está en el borrador (publicar falla hasta corregirlo). `published`: está en la "
        "última versión publicada. `in_use`: lo que corre hoy en una conexión lo nombra, aunque el borrador y la "
        "última versión ya estén limpios."
    )
    field: str = Field(
        description="Dónde del contenido del agente está (p. ej. `tools[3].tool`), para señalar el control en el "
        "editor. Es un localizador técnico: no se muestra al dueño."
    )
    kind: Literal["action", "procedure"]
    name: str = Field(description="Cómo se llamaba lo retirado (p. ej. `informar_plan`).")
    message: str = Field(description="Qué ya no existe y qué hacer, en palabras simples. Es el texto que se muestra.")


class AgentRetiredNotices(BaseModel):
    agent_id: uuid.UUID
    key: str
    name: str
    published_version: int | None = Field(description="Número de la última versión publicada (null si nunca se publicó).")
    in_use: bool = Field(description="Una conexión viva de tu negocio corre hoy algo que nombra lo retirado.")
    blocks_publish: bool = Field(description="El borrador nombra algo retirado: publicar da 422 hasta corregirlo.")
    notices: list[RetiredNotice]


class RetiredReferencesOut(BaseModel):
    affected_agents: int
    agents: list[AgentRetiredNotices] = Field(description="Solo los agentes con al menos un aviso.")


def _avisos_out(a: Any) -> AgentRetiredNotices:
    return AgentRetiredNotices(
        agent_id=a.agent_id,
        key=a.key,
        name=a.name,
        published_version=a.version_publicada,
        in_use=a.en_uso,
        blocks_publish=a.bloquea_publicar,
        notices=[
            RetiredNotice(scope=x.ambito, field=x.campo, kind=x.tipo, name=x.nombre, message=x.texto)
            for x in a.avisos
        ],
    )


async def _avisos(tid: uuid.UUID, agent_id: uuid.UUID | None = None) -> list[Any]:
    from core.releases.avisos_referencias import avisos_del_negocio

    nombres = {a.id: a.name for a, _ in await svc.listar_agentes(tid)}
    return await avisos_del_negocio(tid, nombres, agent_id=agent_id)


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
    if not actua_como_master(principal.admin) and not await svc.escritura_habilitada(tid):
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
    retiradas = [i for i in issues if i.code == CODIGO_AVISO]
    if not retiradas:
        return ApiError(422, "invalid_spec", f"El spec tiene {len(issues)} problema(s) del contrato.", fields=campos)
    # HU-AOS-59: lo retirado va en palabras simples (qué ya no existe y qué hacer), con el mismo texto
    # que el aviso del editor. Mismo 422 `invalid_spec` y mismo `fields`; el aviso suma `details`.
    if len(retiradas) == len(issues) == 1:
        mensaje = retiradas[0].message
    elif len(retiradas) == len(issues):
        mensaje = f"Este agente usa {len(retiradas)} cosas que ya no existen. Revisa cada punto marcado y vuelve a publicar."
    else:
        mensaje = f"El spec tiene {len(issues)} problema(s) del contrato; revisa también lo que ya no existe, marcado abajo."
    return ApiError(
        422,
        "invalid_spec",
        mensaje,
        fields=campos,
        details={"retired_references": [{"field": i.path, "message": i.message} for i in retiradas]},
    )


async def _exigir_enfoque(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """HU-AOS-51: publicar por esta ruta respeta el enfoque del agente igual que el editor (ADR-0155):
    un superpoder fuera de la plantilla del agente da 422 `superpoder_fuera_de_enfoque` por campo.
    Los agentes que no vienen de una plantilla (general, coordinador, por API) no se restringen."""
    from core.agents import editor as editor_svc

    agente = await svc.obtener_agente(tenant_id, agent_id)
    permitidos = editor_svc.procedimientos_del_enfoque(agente.key)
    if permitidos is None:
        return
    borrador = await svc.obtener_borrador(tenant_id, agent_id)
    refs = borrador.content.get("procedures") if isinstance(borrador.content, dict) else None
    issues = [
        Issue(
            "superpoder_fuera_de_enfoque",
            f"procedures[{i}].id",
            f"«{ref.get('id')}» no es parte del enfoque de este agente. Quítalo para poder publicar.",
        )
        for i, ref in enumerate(refs if isinstance(refs, list) else [])
        if isinstance(ref, dict) and ref.get("id") not in permitidos
    ]
    if issues:
        raise svc.ErroresDeContrato(issues)


async def _exigir_sin_retiradas(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """HU-AOS-59: publicar un borrador que nombra algo retirado da 422 con el aviso en palabras
    simples. Un procedimiento retirado no lo detecta el linter del contrato: sin esto la versión
    saldría publicada y el build de la release fallaría después."""
    from core.agents.referencias_retiradas import binario_de

    borrador = await svc.obtener_borrador(tenant_id, agent_id)
    problemas = problemas_de_publicacion(borrador.content, binario_de(plugin_manager.list_modules()))
    if problemas:
        raise svc.ErroresDeContrato(problemas)


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


# Antes de `/{agent_id}`: un `UUID` capturaría `retired-references` y daría 422.
@router.get("/retired-references", response_model=RetiredReferencesOut)
async def list_retired_references(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> RetiredReferencesOut:
    """Avisos del negocio (HU-AOS-59, ADR-0184): por agente, lo que nombra y ya no existe, en palabras
    simples. Solo lectura; solo ve los agentes de su negocio. Solo salen los agentes con avisos."""
    afectados = [a for a in await _avisos(_tenant(principal)) if a.avisos]
    return RetiredReferencesOut(affected_agents=len(afectados), agents=[_avisos_out(a) for a in afectados])


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


@router.get("/{agent_id}/retired-references", response_model=AgentRetiredNotices)
async def get_retired_references(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("settings"))]
) -> AgentRetiredNotices:
    """Aviso de un agente para el editor y el flujo de publicar (mismo texto que el 422 de publicar).
    Sin avisos, `notices` viene vacío y `blocks_publish` en false. 404 si no es un agente del negocio."""
    tid = _tenant(principal)
    try:
        await svc.obtener_agente(tid, agent_id)
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    encontrados = await _avisos(tid, agent_id)
    if not encontrados:  # archivado: no se audita
        raise ApiError.not_found("Agente no encontrado.")
    return _avisos_out(encontrados[0])


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
        await _exigir_enfoque(tid, agent_id)
        await _exigir_sin_retiradas(tid, agent_id)
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
    return AgentVersionDetail(**_version(v).model_dump(), content=v.content, procedures=await _resueltos(v))


async def _resueltos(v: Any) -> list[ResolvedProcedure]:
    """Los `procedures` de un AgentSpec v3 resueltos contra los packs habilitados del negocio
    (el binario de hoy). Vacío para un KernelSpec."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.agents.tolerante import procedimientos_publicados
    from core.releases.procedimientos import publicados_por_id, resolver_refs

    refs = v.content.get("procedures") if v.contract == "agentspec/3" else None
    if not isinstance(refs, list) or not refs:
        return []
    spec = await load_active_spec()
    publicados = publicados_por_id(procedimientos_publicados(spec))
    salida: list[ResolvedProcedure] = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not isinstance(ref.get("version"), str):
            continue
        resueltos, problemas = resolver_refs([ref], abiertos=(), publicados=publicados, habilidades=enabled_packs(spec))
        salida.append(
            ResolvedProcedure(
                id=ref["id"],
                range=ref["version"],
                resolved_version=resueltos[0].version if resueltos else None,
                problem=problemas[0] if problemas else None,
            )
        )
    return salida

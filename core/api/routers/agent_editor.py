"""Editor de agentes desde plantillas (HU-AOS-24, adenda de ADR-0139 del 2026-10-18).

Completa la API de agentes (`agents.py`: alta, borrador con `lock_version`, versiones) con lo que
el editor del panel necesita y no existía:

- `GET  /api/v1/admin/agent-templates` — plantillas (Ventas, Cobranza, Agenda, Suscripciones)
  con sus superpoderes y frases de la habilidad, y si el negocio puede usarlas.
- `POST /api/v1/admin/agent-templates/{key}/agents` — `{name?}` → especialista con la key de la
  plantilla y su borrador derivado (sin publicar). 409 `key_taken` (con `fields.agent_id`) si
  ya existe; 409 `template_unavailable` si faltan habilidades.
- `GET  /api/v1/admin/agent-superpowers` — superpoderes que ofrece el negocio con sus frases.
- `GET  /api/v1/admin/agent-models` — modelos de la tabla de precios del core, cuáles sirve el
  proveedor del negocio, si hay un modelo BYOK que manda y el costo estimado por 1.000 turnos
  con el promedio real de tokens por turno del negocio (30 días); sin historial, `null`.
- `GET  /api/v1/admin/agents/{id}/publish-targets` — conexiones donde se puede instalar.
- `POST /api/v1/admin/agents/{id}/publish` — `{connection_ids, lock_version?, notes?}`. D-35:
  sin al menos una conexión no se publica. Publica la versión, instala el agente y construye la
  release candidata de cada conexión, SIN activarla (el botón «Activar» del panel usa el
  promote de la API de releases, solo del propietario). 422 por campo antes de escribir nada.

Rutas con prefijos propios (`/agent-templates`, ...) para no depender del orden de los routers
frente a `/admin/agents/{agent_id}`. Toda lista va paginada (`page`, `page_size`).

Permisos (ADR-0136): lectura con la sección `settings`; crear con `settings` en escritura;
publicar, además, `connections` en escritura (construye releases, como su API) y las
conexiones visibles. Como el resto del Agent OS, con `aos_runtime = 'legacy'` solo escribe el
master (403 `aos_runtime_legacy`).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, ConfigDict, Field

from core.admin.permissions import effective_access
from core.admin.roles import actua_como_master
from core.agents import editor as svc
from core.agents import versiones
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from core.api.routers.agents import (
    AgentDraft,
    AgentSummary,
    AgentVersionSummary,
    _catalogo,
    _contrato_422,
    _draft,
    _puede_escribir,
    _summary,
    _tenant,
    _version,
)

router = APIRouter(prefix="/admin", tags=["admin · editor de agentes"])


# ------------------------------------------------------------------ esquemas
class Superpower(BaseModel):
    id: str
    version: str
    skill: str
    title: str
    description: str
    phrases: list[str]


class AgentTemplate(BaseModel):
    key: str
    name: str
    description: str
    skills: list[str]
    optional_skills: list[str]
    available: bool
    missing_skills: list[str]
    superpowers: list[Superpower]


class FromTemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)


class FromTemplateOut(BaseModel):
    agent: AgentSummary
    draft: AgentDraft
    warnings: list[str]


class LlmModel(BaseModel):
    provider: str
    model: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    selectable: bool
    retired: bool
    estimated_usd_per_1000_turns: float | None


class LlmModelCatalog(Page[LlmModel]):
    provider: str
    byok_model: str | None
    avg_input_tokens: float | None
    avg_output_tokens: float | None
    sample_turns: int
    min_phrases: int


class PublishTarget(BaseModel):
    connection_id: uuid.UUID
    label: str
    channel: str
    installed: bool
    covers_skills: bool
    missing_skills: list[str]
    active_release_id: uuid.UUID | None


class EditorPublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # D-35: al menos una conexión. El tope y los repetidos los revisa el editor (error por campo).
    connection_ids: list[uuid.UUID] = Field(min_length=1, max_length=svc.MAX_CONEXIONES)
    lock_version: int | None = Field(default=None, ge=0, lt=2**31)
    label: str | None = Field(default=None, max_length=60)
    notes: str = Field(default="", max_length=2000)


class PublishedConnection(BaseModel):
    connection_id: uuid.UUID
    instance: str
    release_id: uuid.UUID | None
    release_number: int | None
    release_created: bool | None
    error: str | None


class EditorPublishOut(BaseModel):
    version: AgentVersionSummary
    created: bool
    connections: list[PublishedConnection]
    warnings: list[str]


# ------------------------------------------------------------------ utilidades
def _superpower(s: svc.Superpoder) -> Superpower:
    return Superpower(
        id=s.id, version=s.version, skill=s.pack, title=s.title, description=s.description, phrases=list(s.phrases)
    )


def _error(exc: svc.ErrorDelEditor) -> ApiError:
    return ApiError(exc.status, exc.code, str(exc), fields=exc.campos or None)


async def _ve_conexiones(principal: Principal, tid: uuid.UUID, *, write: bool = False) -> None:
    if actua_como_master(principal.admin):
        return
    acceso = await effective_access(principal.admin, "connections", tid)
    if acceso is None or (write and acceso != "write"):
        raise ApiError.forbidden("Sin permiso para la sección 'connections'.")


Paginados = Annotated[PageParams, Depends(page_params)]


# ------------------------------------------------------------------ rutas
@router.get("/agent-templates", response_model=Page[AgentTemplate])
async def list_templates(
    principal: Annotated[Principal, Depends(require("settings"))], params: Paginados
) -> Page[AgentTemplate]:
    vistas = svc.plantillas(await svc.habilitados_del_negocio(_tenant(principal)))
    items = [
        AgentTemplate(
            key=v.key,
            name=v.name,
            description=v.description,
            skills=list(v.skills),
            optional_skills=list(v.optional_skills),
            available=v.available,
            missing_skills=list(v.missing_skills),
            superpowers=[_superpower(s) for s in v.superpowers],
        )
        for v in vistas
    ]
    return build_page(items[params.offset : params.offset + params.page_size], len(items), params)


@router.post(
    "/agent-templates/{template_key}/agents", response_model=FromTemplateOut, status_code=status.HTTP_201_CREATED
)
async def create_from_template(
    template_key: Annotated[str, Path(pattern=r"^[a-z0-9_]{2,40}$")],
    payload: FromTemplateIn,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> FromTemplateOut:
    tid = await _puede_escribir(principal)
    try:
        agente, borrador, avisos = await svc.crear_desde_plantilla(
            tid, template_key, nombre=payload.name, catalogo=_catalogo(), actor=principal.admin.id
        )
    except svc.ErrorDelEditor as exc:
        raise _error(exc) from None
    except versiones.ErroresDeContrato as exc:
        raise _contrato_422(exc.issues) from None
    return FromTemplateOut(agent=_summary(agente, None), draft=_draft(borrador), warnings=avisos)


@router.get("/agent-superpowers", response_model=Page[Superpower])
async def list_superpowers(
    principal: Annotated[Principal, Depends(require("settings"))], params: Paginados
) -> Page[Superpower]:
    items = [_superpower(s) for s in svc.superpoderes(await svc.habilitados_del_negocio(_tenant(principal)))]
    return build_page(items[params.offset : params.offset + params.page_size], len(items), params)


@router.get("/agent-models", response_model=LlmModelCatalog)
async def list_models(
    principal: Annotated[Principal, Depends(require("settings"))], params: Paginados
) -> LlmModelCatalog:
    ctx = await svc.contexto_de_modelos(_tenant(principal))
    items = [
        LlmModel(
            provider=m.provider,
            model=m.model,
            input_usd_per_mtok=m.input_usd_per_mtok,
            output_usd_per_mtok=m.output_usd_per_mtok,
            selectable=m.selectable,
            retired=m.retired,
            estimated_usd_per_1000_turns=m.estimated_usd_per_1000_turns,
        )
        for m in ctx.models
    ]
    pagina = build_page(items[params.offset : params.offset + params.page_size], len(items), params)
    return LlmModelCatalog(
        **pagina.model_dump(),
        provider=ctx.provider,
        byok_model=ctx.byok_model,
        avg_input_tokens=ctx.avg_input_tokens,
        avg_output_tokens=ctx.avg_output_tokens,
        sample_turns=ctx.sample_turns,
        min_phrases=svc.MIN_FRASES,
    )


@router.get("/agents/{agent_id}/publish-targets", response_model=Page[PublishTarget])
async def list_publish_targets(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("settings"))],
    params: Paginados,
) -> Page[PublishTarget]:
    tid = _tenant(principal)
    await _ve_conexiones(principal, tid)
    try:
        borrador = await versiones.obtener_borrador(tid, agent_id)
    except versiones.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    destinos = await svc.destinos(
        tid, agent_id, svc.habilidades_del_contenido(borrador.content), await svc.habilitados_del_negocio(tid)
    )
    items = [
        PublishTarget(
            connection_id=d.connection_id,
            label=d.label,
            channel=d.channel,
            installed=d.installed,
            covers_skills=d.covers_skills,
            missing_skills=list(d.missing_skills),
            active_release_id=d.active_release_id,
        )
        for d in destinos
    ]
    return build_page(items[params.offset : params.offset + params.page_size], len(items), params)


@router.post("/agents/{agent_id}/publish", response_model=EditorPublishOut)
async def publish_to_connections(
    agent_id: uuid.UUID,
    payload: EditorPublishIn,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> EditorPublishOut:
    tid = await _puede_escribir(principal)
    await _ve_conexiones(principal, tid, write=True)
    try:
        resultado = await svc.publicar_en_conexiones(
            tid,
            agent_id,
            payload.connection_ids,
            catalogo=_catalogo(),
            admin_id=principal.admin.id,
            actor=f"admin:{principal.admin.id}",
            lock_version=payload.lock_version,
            notes=payload.notes,
            label=payload.label,
        )
    except versiones.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    except svc.ErrorDelEditor as exc:
        raise _error(exc) from None
    except versiones.ErroresDeContrato as exc:
        raise _contrato_422(exc.issues) from None
    except versiones.Conflicto as exc:
        raise ApiError(409, "conflict", str(exc)) from None
    return EditorPublishOut(
        version=_version(resultado.version),
        created=resultado.created,
        connections=[
            PublishedConnection(
                connection_id=c.connection_id,
                instance=c.instance,
                release_id=c.release_id,
                release_number=c.release_number,
                release_created=c.release_created,
                error=c.error,
            )
            for c in resultado.connections
        ],
        warnings=resultado.warnings,
    )


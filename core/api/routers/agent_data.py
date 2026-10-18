"""Mi agente y su data (HU-AOS-47, ADR-0140), solo lectura.

- `GET /api/v1/admin/agents/overview` — runtime del negocio, agentes (sin el Coordinador) con sus
  conexiones y actividad de 30 días, y lo no atribuido aparte (`sin_atribuir`).
- `GET /api/v1/admin/agents/{ref}/conversations` — conversaciones que atendió (conexión y versión
  del último turno), paginadas.
- `GET /api/v1/admin/agents/{ref}/customers` — clientes que atendió, paginados.
- `GET /api/v1/admin/agents/{ref}/tasks?state=open|closed` — tareas de las que es dueño.
- `GET /api/v1/admin/agents/{ref}/records?kind=` — pedidos, leads, citas y promesas que creó.
- `GET /api/v1/admin/agents/{ref}/usage` — turnos, tokens y costo del período, por conexión y por
  día, y la consistencia pass^k vigente del negocio con su tendencia.

`ref` es el id del agente o el literal `general` (el General absorbe lo no atribuido; ver
`core.agents.data_por_agente`). Filtros comunes: `connection_id`, `desde`, `hasta` (por defecto
30 días, máximo 366).

Permisos (ADR-0136): sección `agents`. Conversaciones y clientes exigen además `conversations`
(nombres de clientes); un colaborador sin ella recibe 403 y las tareas le llegan sin
`conversation_id`. Los registros se filtran por el permiso del pack dueño de cada uno. Un agente
de otro negocio es 404 sin eco del id. Este router se incluye ANTES del de `agents` para que
`/overview` no lo capture `/{agent_id}` (test que lo cubre: `test_agent_data_api.py`).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.admin.permissions import effective_access
from core.agents import data_por_agente as svc
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from core.plugin_manager import plugin_manager

router = APIRouter(prefix="/admin/agents", tags=["admin · agentes"])

_REF_RE = re.compile(r"general|[0-9a-fA-F-]{36}")
_KIND_PATTERN = r"^[a-z0-9_]{1,40}$"


# ------------------------------------------------------------------ esquemas
class AgentConnection(BaseModel):
    id: uuid.UUID
    label: str
    enabled: bool


class Activity(BaseModel):
    turns: int
    conversations: int
    cost_usd: float
    last_activity_at: datetime | None


class AgentOverviewItem(Activity):
    id: uuid.UUID | None
    ref: str
    key: str
    name: str
    kind: str
    status: str
    virtual: bool
    published: bool
    connections: list[AgentConnection]
    incluye_sin_atribuir: bool


class AgentsOverview(BaseModel):
    runtime: str
    items: list[AgentOverviewItem]
    sin_atribuir: Activity


class AgentConversation(BaseModel):
    conversation_id: uuid.UUID
    customer_id: uuid.UUID | None
    customer_name: str | None
    channel: str | None
    status: str | None
    connection_id: uuid.UUID | None
    connection_label: str | None
    agent_version: int | None
    turns: int
    cost_usd: float
    last_turn_at: datetime


class AgentCustomer(BaseModel):
    customer_id: uuid.UUID
    name: str | None
    channel: str | None
    conversations: int
    turns: int
    last_turn_at: datetime


class AgentTask(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID | None
    procedure_id: str
    procedure_version: str
    status: str
    open: bool
    status_reason: str | None
    stage: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    connection_id: uuid.UUID | None


class AgentRecordItem(BaseModel):
    id: uuid.UUID
    pack_id: str
    kind: str
    record_ref: str
    label: str | None
    tool: str
    conversation_id: uuid.UUID | None
    connection_id: uuid.UUID | None
    connection_label: str | None
    agent_version: int | None
    created_at: datetime


class UsageTotals(BaseModel):
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class UsageByConnection(UsageTotals):
    connection_id: uuid.UUID | None
    connection_label: str | None


class UsageByDay(UsageTotals):
    day: date


class UsagePeriod(BaseModel):
    desde: datetime
    hasta: datetime


class ConsistencyMeasurement(BaseModel):
    measured_at: datetime
    k: int
    pass_1: float | None
    pass_k: float
    spec_version: str | None


class Consistency(BaseModel):
    scope: Literal["negocio"]
    measurements: list[ConsistencyMeasurement]


class AgentUsage(BaseModel):
    periodo: UsagePeriod
    incluye_sin_atribuir: bool
    total: UsageTotals
    by_connection: list[UsageByConnection]
    by_day: list[UsageByDay]
    consistency: Consistency


# ------------------------------------------------------------------ utilidades
def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _alcance(principal: Principal, ref: str) -> svc.Alcance:
    try:
        return await svc.resolver(_tenant(principal), ref)
    except svc.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None


def _filtros(
    desde: datetime | None = None,
    hasta: datetime | None = None,
    connection_id: uuid.UUID | None = None,
) -> svc.Filtros:
    try:
        return svc.Filtros(periodo=svc.periodo(desde, hasta), connection_id=connection_id)
    except svc.PeriodoInvalido as exc:
        raise ApiError.bad_request(str(exc), code="invalid_period") from None


async def _puede(principal: Principal, seccion: str) -> bool:
    return await effective_access(principal.admin, seccion, principal.tenant_id) is not None


async def _exigir_conversaciones(principal: Principal) -> None:
    if not await _puede(principal, "conversations"):
        raise ApiError.forbidden("Sin permiso para la sección 'conversations'.")


async def _packs_visibles(principal: Principal) -> frozenset[str]:
    """Packs cuyo permiso (sección del pack, ADR-0063) tiene el admin; `core` siempre."""
    visibles = {"core"}
    for module in plugin_manager.list_modules():
        pack_id = getattr(module, "id", None)
        if isinstance(pack_id, str) and pack_id and await _puede(principal, pack_id):
            visibles.add(pack_id)
    return frozenset(visibles)


def _ref(ref: str) -> str:
    """El id de un agente o `general`; cualquier otra cosa es «no encontrado», igual que un id
    ajeno (sin 422 que distinga formatos)."""
    if not _REF_RE.fullmatch(ref):
        raise ApiError.not_found("Agente no encontrado.")
    return ref.lower()


# ------------------------------------------------------------------ rutas
@router.get("/overview", response_model=AgentsOverview)
async def agents_overview(principal: Annotated[Principal, Depends(require("agents"))]) -> AgentsOverview:
    return AgentsOverview.model_validate(await svc.resumen(_tenant(principal)))


@router.get("/{ref}/conversations", response_model=Page[AgentConversation])
async def agent_conversations(
    ref: str,
    principal: Annotated[Principal, Depends(require("agents"))],
    filtros: Annotated[svc.Filtros, Depends(_filtros)],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[AgentConversation]:
    alcance = await _alcance(principal, _ref(ref))
    await _exigir_conversaciones(principal)
    pagina = await svc.conversaciones(alcance, filtros, offset=params.offset, limit=params.page_size)
    return build_page([AgentConversation(**i) for i in pagina.items], pagina.total, params)


@router.get("/{ref}/customers", response_model=Page[AgentCustomer])
async def agent_customers(
    ref: str,
    principal: Annotated[Principal, Depends(require("agents"))],
    filtros: Annotated[svc.Filtros, Depends(_filtros)],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[AgentCustomer]:
    alcance = await _alcance(principal, _ref(ref))
    await _exigir_conversaciones(principal)
    pagina = await svc.clientes(alcance, filtros, offset=params.offset, limit=params.page_size)
    return build_page([AgentCustomer(**i) for i in pagina.items], pagina.total, params)


@router.get("/{ref}/tasks", response_model=Page[AgentTask])
async def agent_tasks(
    ref: str,
    principal: Annotated[Principal, Depends(require("agents"))],
    filtros: Annotated[svc.Filtros, Depends(_filtros)],
    params: Annotated[PageParams, Depends(page_params)],
    state: Literal["open", "closed"] | None = None,
) -> Page[AgentTask]:
    alcance = await _alcance(principal, _ref(ref))
    ver_conversaciones = await _puede(principal, "conversations")
    pagina = await svc.tareas(alcance, filtros, estado=state, offset=params.offset, limit=params.page_size)
    items: list[AgentTask] = []
    for item in pagina.items:
        datos: dict[str, Any] = dict(item)
        if not ver_conversaciones:
            datos["conversation_id"] = None
        items.append(AgentTask(**datos))
    return build_page(items, pagina.total, params)


@router.get("/{ref}/records", response_model=Page[AgentRecordItem])
async def agent_records(
    ref: str,
    principal: Annotated[Principal, Depends(require("agents"))],
    filtros: Annotated[svc.Filtros, Depends(_filtros)],
    params: Annotated[PageParams, Depends(page_params)],
    kind: Annotated[str | None, Query(pattern=_KIND_PATTERN)] = None,
) -> Page[AgentRecordItem]:
    alcance = await _alcance(principal, _ref(ref))
    ver_conversaciones = await _puede(principal, "conversations")
    pagina = await svc.registros(
        alcance,
        filtros,
        packs_visibles=await _packs_visibles(principal),
        kind=kind,
        offset=params.offset,
        limit=params.page_size,
    )
    items: list[AgentRecordItem] = []
    for item in pagina.items:
        datos: dict[str, Any] = dict(item)
        if not ver_conversaciones:
            datos["conversation_id"] = None
        items.append(AgentRecordItem(**datos))
    return build_page(items, pagina.total, params)


@router.get("/{ref}/usage", response_model=AgentUsage)
async def agent_usage(
    ref: str,
    principal: Annotated[Principal, Depends(require("agents"))],
    filtros: Annotated[svc.Filtros, Depends(_filtros)],
) -> AgentUsage:
    alcance = await _alcance(principal, _ref(ref))
    return AgentUsage.model_validate(await svc.consumo(alcance, filtros))

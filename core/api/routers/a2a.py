"""Permisos de colaboración entre agentes y su ledger (ADR-0116, adenda HU-AOS-18).

- `GET /api/v1/admin/a2a/graph` — la versión vigente del grafo (la que congelará la próxima
  release de cada conexión), sus enlaces y advertencias (ciclos de `handoff`).
- `PUT /api/v1/admin/a2a/graph` — publica una versión nueva con la lista completa de enlaces.
  Idempotente: con el mismo contenido devuelve la vigente (`created: false`). 422 con
  `modo_no_permitido` (broadcast/colaboración son internos del Coordinador), `autollamada`,
  `enlace_repetido`, `agente_desconocido` o `enlace_invalido`.
- `GET /api/v1/admin/a2a/graph/versions` — historial.
- `GET /api/v1/admin/a2a/calls?connection_id=` — una fila por llamada (`call_id`), sin payload.

Permisos: la sección `settings`, como los agentes. Publicar no llega a clientes reales hasta que se
promueve una release (que pide propietario o master); mientras el negocio esté en `legacy`, las
escrituras quedan para el master (403 `aos_runtime_legacy`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.admin.roles import actua_como_master
from core.agents import grafo_a2a as svc
from core.agents.versiones import escritura_habilitada
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/admin/a2a", tags=["admin · colaboración entre agentes"])


class Link(BaseModel):
    from_agent_id: uuid.UUID
    to_agent_id: uuid.UUID
    modes: list[str]
    intents: list[str]
    max_depth: int
    timeout_ms: int
    budget_usd: float
    budget_tokens: int
    data_classes: list[str]


class GraphVersion(BaseModel):
    id: uuid.UUID
    version: int
    content_hash: str
    created_at: datetime


class Graph(BaseModel):
    version: GraphVersion | None
    links: list[Link]
    warnings: list[str] = Field(description="Ciclos de `handoff` (`ciclo_handoff:a>b`): no bloquean.")


class GraphIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    links: list[dict[str, Any]] = Field(max_length=200)


class GraphOut(Graph):
    created: bool


class Call(BaseModel):
    call_id: uuid.UUID
    connection_id: uuid.UUID
    conversation_id: uuid.UUID | None
    mode: str
    from_instance_id: uuid.UUID
    to_instance_id: uuid.UUID | None
    intent: str
    depth: int
    status: str = Field(description="Último evento de la llamada (`accepted`, `completed`, `denied`, …).")
    reason: str | None
    tokens: int
    cost_usd: float
    started_at: datetime
    ended_at: datetime


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


def _link(e: svc.EnlaceA2A) -> Link:
    return Link(**e.model_dump())


def _version(v: svc.VersionDelGrafo) -> GraphVersion:
    return GraphVersion(id=v.id, version=v.version, content_hash=v.content_hash, created_at=v.created_at)


@router.get("/graph", response_model=Graph)
async def get_graph(principal: Annotated[Principal, Depends(require("settings"))]) -> Graph:
    vigente = await svc.vigente(_tenant(principal))
    if vigente is None:
        return Graph(version=None, links=[], warnings=[])
    return Graph(
        version=_version(vigente),
        links=[_link(e) for e in vigente.enlaces],
        warnings=svc.advertencias(vigente.enlaces),
    )


@router.put("/graph", response_model=GraphOut)
async def put_graph(
    payload: GraphIn, principal: Annotated[Principal, Depends(require("settings", write=True))]
) -> GraphOut:
    tid = _tenant(principal)
    if not actua_como_master(principal.admin) and not await escritura_habilitada(tid):
        raise ApiError(
            403,
            "aos_runtime_legacy",
            "Los permisos entre agentes se activan con el Agent OS de este negocio: por ahora solo los edita el administrador.",
        )
    try:
        version, creada = await svc.publicar(tid, payload.links, created_by=principal.admin.id)
    except ValidationError as exc:
        primero = exc.errors()[0] if exc.errors() else {}
        raise ApiError(
            422,
            svc.codigo_de_error(exc),
            "Un permiso no es válido.",
            fields={".".join(str(p) for p in primero.get("loc", ())) or "links": str(primero.get("msg", ""))[:200]},
        ) from None
    except svc.ErrorDelGrafo as exc:
        raise ApiError(422, exc.codigo, str(exc)) from None
    except DBAPIError as exc:
        log.error("a2a_graph_db_error", error=type(exc.orig).__name__)
        raise ApiError(500, "db_error", "No se pudo guardar. Inténtalo de nuevo.") from None
    return GraphOut(
        version=_version(version),
        links=[_link(e) for e in version.enlaces],
        warnings=svc.advertencias(version.enlaces),
        created=creada,
    )


@router.get("/graph/versions", response_model=list[GraphVersion])
async def list_graph_versions(
    principal: Annotated[Principal, Depends(require("settings"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[GraphVersion]:
    return [_version(v) for v in await svc.versiones(_tenant(principal), limite=limit)]


@router.get("/calls", response_model=list[Call])
async def list_calls(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("settings"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> list[Call]:
    tid = _tenant(principal)
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "WITH llamadas AS ("
                    "  SELECT e.call_id, c.connection_id, c.conversation_id, "
                    "    (array_agg(e.mode ORDER BY e.seq))[1] AS mode, "
                    "    (array_agg(e.from_instance_id ORDER BY e.seq))[1] AS from_instance_id, "
                    "    (array_agg(e.to_instance_id ORDER BY e.seq))[1] AS to_instance_id, "
                    "    (array_agg(e.intent ORDER BY e.seq))[1] AS intent, "
                    "    max(e.depth) AS depth, "
                    "    (array_agg(e.type ORDER BY e.seq DESC))[1] AS status, "
                    "    (array_agg(e.reason ORDER BY e.seq DESC))[1] AS reason, "
                    "    sum(e.tokens_in + e.tokens_out) AS tokens, sum(e.cost_usd) AS cost_usd, "
                    "    min(e.created_at) AS started_at, max(e.created_at) AS ended_at "
                    "  FROM a2a_events e JOIN a2a_conversations c "
                    "    ON c.tenant_id = e.tenant_id AND c.id = e.a2a_conversation_id "
                    "  WHERE e.tenant_id = :t AND c.connection_id = :c "
                    "  GROUP BY e.call_id, c.connection_id, c.conversation_id"
                    ") SELECT * FROM llamadas WHERE (cast(:antes AS timestamptz) IS NULL OR started_at < :antes) "
                    "ORDER BY started_at DESC LIMIT :n"
                ),
                {"t": tid, "c": connection_id, "antes": before, "n": limit},
            )
        ).mappings()
        return [Call(**{**f, "tokens": int(f["tokens"] or 0), "cost_usd": float(f["cost_usd"] or 0)}) for f in filas]

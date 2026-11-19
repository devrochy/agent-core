"""Tablero de fiabilidad (HU-AOS-29, adenda de ADR-0117). Solo lectura, salvo la marca de falso positivo.

- `GET /api/v1/admin/reliability`: métricas operativas, rechazos del verificador por tipo,
  completitud por procedimiento y consistencia vigente por conexión;
- `GET /api/v1/admin/reliability/verifier`: los turnos con un rechazo del verificador;
- `POST /api/v1/admin/reliability/verifier/{turn_id}/false-positive`: el dueño marca un rechazo
  como falso positivo (idempotente, no se desmarca);
- `GET /api/v1/admin/usage?group_by=connection|agent|a2a|day`: gasto del ledger agrupado;
- `GET /api/v1/admin/evals/runs`: las últimas corridas de evaluación, sin su costo.

Permisos (ADR-0136): sección `agents` (escritura para marcar). El dinero solo sale con `costs`; si
no, `null`. El `conversation_id` solo sale con `conversations`. Todo corre en el `tenant_scope` de la
request y filtra `tenant_id` explícito: una conexión, versión o turno de otro negocio da vacío o 404,
sin eco del id. Bajo la muestra mínima las tasas salen `null` («sin datos suficientes»).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.admin.permissions import effective_access
from core.agents import data_por_agente as periodos
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.session import get_session
from core.metricas import fiabilidad as svc

router = APIRouter(prefix="/admin", tags=["admin · fiabilidad"])

_TIPO_PATTERN = r"^[a-z0-9_]{1,60}$"
_FRACCION = "Fracción de 0 a 1; null bajo la muestra mínima."
Lane = Literal["stable", "canary", "pinned", "legacy"]


# ------------------------------------------------------------------ esquemas
class ReliabilityPeriod(BaseModel):
    desde: datetime
    hasta: datetime


class MinimumSample(BaseModel):
    turns: int = svc.MIN_TURNOS
    tasks: int = svc.MIN_TAREAS


class AgentReliability(BaseModel):
    instance_id: uuid.UUID | None
    name: str
    turns: int
    escalated: int
    escalation_rate: float | None = Field(description=_FRACCION)
    p95_ms: int | None
    # En USD: solo con la sección `costs`; si no, null.
    cost_usd: float | None


class Operational(BaseModel):
    turns: int
    conversations: int
    escalated: int
    escalated_conversations: int
    failed: int
    without_outcome: int
    escalation_rate: float | None = Field(description=_FRACCION)
    p95_ms: int | None
    # En USD: solo con la sección `costs`; si no, null.
    cost_usd: float | None
    cost_per_conversation_usd: float | None
    by_agent: list[AgentReliability]


class VerifierKind(BaseModel):
    kind: str
    rejections: int
    false_positives: int


class ProcedureCompletion(BaseModel):
    procedure_id: str
    finished: int
    completed: int
    escalated: int
    rate: float | None = Field(description=_FRACCION)


class ProcedureConsistency(BaseModel):
    procedure_id: str
    pass_k: float | None = Field(description="Fracción de escenarios estables en k = 4 (0 a 1); null sin datos.")
    ok: bool | None
    scenarios: int
    measured_in_release_id: uuid.UUID | None
    measured_in_version: int | None


class ConnectionConsistency(BaseModel):
    connection_id: uuid.UUID
    connection: str
    release_id: uuid.UUID | None
    version: int | None
    threshold: float = Field(description="Umbral de pass^4 (0 a 1).")
    no_data: bool
    below_threshold: list[str]
    procedures: list[ProcedureConsistency]


class ReliabilityResponse(BaseModel):
    period: ReliabilityPeriod
    minimum_sample: MinimumSample = Field(default_factory=MinimumSample)
    operational: Operational
    verifier: list[VerifierKind]
    completion: list[ProcedureCompletion]
    # Lo vigente de la versión activa de cada conexión: no se acota al periodo, versión ni carril.
    consistency: list[ConnectionConsistency] = Field(
        description="Consistencia vigente de la versión activa; no se acota al periodo, versión ni carril."
    )
    can_see_costs: bool
    can_see_conversations: bool


class VerifierRejection(BaseModel):
    turn_id: uuid.UUID
    # Solo con la sección `conversations`; si no, null.
    conversation_id: uuid.UUID | None
    connection_id: uuid.UUID | None
    created_at: datetime
    kind: str
    false_positive: bool


class VerifierRejections(BaseModel):
    items: list[VerifierRejection]


class FalsePositiveIn(BaseModel):
    kind: str = Field(pattern=_TIPO_PATTERN)
    note: str | None = Field(default=None, max_length=500)


class FalsePositiveOut(BaseModel):
    turn_id: uuid.UUID
    kind: str
    false_positive: bool = True


class UsageRow(BaseModel):
    key: str | None
    name: str
    mode: str | None
    rows: int
    # En USD: solo con la sección `costs`; si no, null.
    cost_usd: float | None


class UsageResponse(BaseModel):
    period: ReliabilityPeriod
    group_by: Literal["connection", "agent", "a2a", "day"]
    can_see_costs: bool
    items: list[UsageRow]


class ReliabilityEvalRun(BaseModel):
    id: uuid.UUID
    origin: str
    k: int
    pass_1: float | None
    pass_k_average: float | None
    passed: int
    total: int
    started_at: datetime | None
    version: int | None
    connection_id: uuid.UUID | None


class EvalRunsResponse(BaseModel):
    items: list[ReliabilityEvalRun]


# ------------------------------------------------------------------ utilidades
def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _puede(principal: Principal, seccion: str) -> bool:
    return await effective_access(principal.admin, seccion, principal.tenant_id) is not None


def _filtro(
    desde: datetime | None = None,
    hasta: datetime | None = None,
    connection_id: uuid.UUID | None = None,
    release_id: uuid.UUID | None = None,
    lane: Lane | None = None,
) -> svc.Filtro:
    try:
        p = periodos.periodo(desde, hasta)
    except periodos.PeriodoInvalido as exc:
        raise ApiError.bad_request(str(exc), code="invalid_period") from None
    return svc.Filtro(desde=p.desde, hasta=p.hasta, connection_id=connection_id, release_id=release_id, lane=lane)


# ------------------------------------------------------------------ rutas
@router.get("/reliability", response_model=ReliabilityResponse)
async def reliability(
    principal: Annotated[Principal, Depends(require("agents"))],
    filtro: Annotated[svc.Filtro, Depends(_filtro)],
) -> ReliabilityResponse:
    tid = _tenant(principal)
    ver_costos = await _puede(principal, "costs")
    async with get_session() as session:
        op = await svc.operativas(session, tid, filtro)
        verificador = await svc.verificador(session, tid, filtro)
        completitud = await svc.completitud(session, tid, filtro)
        consistencia = await svc.consistencia(session, tid, filtro.connection_id)

    def dinero(valor: float | None) -> float | None:
        return round(valor, 6) if ver_costos and valor is not None else None

    return ReliabilityResponse(
        period=ReliabilityPeriod(desde=filtro.desde, hasta=filtro.hasta),
        operational=Operational(
            turns=op.turnos,
            conversations=op.conversaciones,
            escalated=op.escalados,
            escalated_conversations=op.conversaciones_escaladas,
            failed=op.fallidos,
            without_outcome=op.sin_resultado,
            escalation_rate=op.tasa_escalamiento,
            p95_ms=op.p95_ms,
            cost_usd=dinero(op.costo_usd),
            cost_per_conversation_usd=dinero(op.costo_por_conversacion),
            by_agent=[
                AgentReliability(
                    instance_id=a["instance_id"],
                    name=a["nombre"],
                    turns=a["turnos"],
                    escalated=a["escalados"],
                    escalation_rate=a["tasa_escalamiento"],
                    p95_ms=a["p95_ms"],
                    cost_usd=dinero(a["costo_usd"]),
                )
                for a in op.por_agente
            ],
        ),
        verifier=[
            VerifierKind(kind=v["tipo"], rejections=v["rechazos"], false_positives=v["falsos_positivos"])
            for v in verificador
        ],
        completion=[
            ProcedureCompletion(
                procedure_id=c["procedure_id"],
                finished=c["terminadas"],
                completed=c["completadas"],
                escalated=c["escaladas"],
                rate=c["tasa"],
            )
            for c in completitud
        ],
        consistency=[_consistencia(c) for c in consistencia],
        can_see_costs=ver_costos,
        can_see_conversations=await _puede(principal, "conversations"),
    )


def _consistencia(c: dict[str, Any]) -> ConnectionConsistency:
    return ConnectionConsistency(
        connection_id=c["connection_id"],
        connection=c["conexion"] or "Conexión",
        release_id=c["release_id"],
        version=c["version"],
        threshold=c["umbral"],
        no_data=c["sin_datos"],
        below_threshold=c["bajo_umbral"],
        procedures=[
            ProcedureConsistency(
                procedure_id=p["procedure_id"],
                pass_k=p["pass_k"],
                ok=p["ok"],
                scenarios=p["escenarios"],
                measured_in_release_id=p["medido_en_release_id"],
                measured_in_version=p["medido_en_version"],
            )
            for p in c["procedimientos"]
        ],
    )


@router.get("/reliability/verifier", response_model=VerifierRejections)
async def verifier_rejections(
    principal: Annotated[Principal, Depends(require("agents"))],
    filtro: Annotated[svc.Filtro, Depends(_filtro)],
    kind: Annotated[str | None, Query(pattern=_TIPO_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> VerifierRejections:
    tid = _tenant(principal)
    ver_conversaciones = await _puede(principal, "conversations")
    async with get_session() as session:
        filas = await svc.rechazos_del_verificador(session, tid, filtro, tipo=kind, limite=limit)
    return VerifierRejections(
        items=[
            VerifierRejection(
                turn_id=r["turn_id"],
                conversation_id=r["conversation_id"] if ver_conversaciones else None,
                connection_id=r["connection_id"],
                created_at=r["creado"],
                kind=r["tipo"],
                false_positive=r["falso_positivo"],
            )
            for r in filas
        ]
    )


@router.post("/reliability/verifier/{turn_id}/false-positive", response_model=FalsePositiveOut)
async def mark_false_positive(
    turn_id: uuid.UUID,
    body: FalsePositiveIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> FalsePositiveOut:
    tid = _tenant(principal)
    ok = await svc.marcar_falso_positivo(tid, turn_id, body.kind, actor=f"admin:{principal.admin.id}", nota=body.note)
    if not ok:
        raise ApiError.not_found("No hay un rechazo del verificador de ese tipo en ese turno.")
    return FalsePositiveOut(turn_id=turn_id, kind=body.kind)


@router.get("/usage", response_model=UsageResponse)
async def usage(
    principal: Annotated[Principal, Depends(require("agents"))],
    filtro: Annotated[svc.Filtro, Depends(_filtro)],
    group_by: Literal["connection", "agent", "a2a", "day"] = "agent",
) -> UsageResponse:
    tid = _tenant(principal)
    ver_costos = await _puede(principal, "costs")
    async with get_session() as session:
        filas = await svc.costo(session, tid, filtro, por=group_by)
    return UsageResponse(
        period=ReliabilityPeriod(desde=filtro.desde, hasta=filtro.hasta),
        group_by=group_by,
        can_see_costs=ver_costos,
        items=[
            UsageRow(
                key=f["clave"],
                name=f["nombre"],
                mode=f["modo"],
                rows=f["filas"],
                cost_usd=round(f["costo_usd"], 6) if ver_costos else None,
            )
            for f in filas
        ],
    )


@router.get("/evals/runs", response_model=EvalRunsResponse)
async def eval_runs(
    principal: Annotated[Principal, Depends(require("agents"))],
    connection_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> EvalRunsResponse:
    tid = _tenant(principal)
    async with get_session() as session:
        filas = await svc.corridas(session, tid, connection_id=connection_id, limite=limit)
    return EvalRunsResponse(
        items=[
            ReliabilityEvalRun(
                id=r["id"],
                origin=r["origen"],
                k=r["k"],
                pass_1=r["pass_1"],
                pass_k_average=r["pass_k_promedio"],
                passed=r["aprobados"],
                total=r["total"],
                started_at=r["creada"],
                version=r["version"],
                connection_id=r["connection_id"],
            )
            for r in filas
        ]
    )

"""Depurador del turno (HU-AOS-28, ADR-0169 §4).

`GET /api/v1/admin/turns/{turn_id}`: la línea de tiempo completa de un turno (decisión, pasos,
cambios de estado, comprobaciones, operaciones entre agentes, envío y costo) en una lectura.

Permisos (ADR-0136, ADR-0169 §2): la sección `conversations` en lectura. La entrada y la salida de
cada paso solo salen con `observability` y siempre redactadas (datos personales enmascarados); sin
ella van `null` con `detalle_oculto`. El dinero solo con `costs`. Corre en el `tenant_scope` de la
request: un turno de otro negocio da 404 sin eco del id.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.admin.permissions import effective_access
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.session import get_session
from core.observability import turno as svc

router = APIRouter(prefix="/admin", tags=["admin · depurador"])

StepKind = Literal["comando", "accion", "motor", "escalacion", "verificador", "guardrail", "otro"]
_REDACTADO = "Redactado: los datos personales van enmascarados (ADR-0169)."


class TurnAgent(BaseModel):
    instance_id: uuid.UUID
    name: str


class TurnDecision(BaseModel):
    outcome: str = Field(description="routed | blocked | escalated | ignored")
    blocked_by: str | None
    route_source: str | None
    rule_id: str | None
    candidates: list[dict[str, Any]] = Field(description="Procedimientos candidatos: `{procedure_id, owner, score}`.")
    confidence: float | None
    active_agent: TurnAgent | None
    final_agent: TurnAgent | None = Field(description="Quien terminó el turno (distinto si hubo un traspaso).")
    shadow_stack: Any = Field(None, description="Plan de la pila de tareas en sombra (HU-AOS-20), si lo hubo.")


class TurnStep(BaseModel):
    at: datetime
    kind: StepKind
    actor: str
    name: str = Field(description="Nombre de la acción, el comando o el componente.")
    action: str = Field(description="execute | denied | confirmation_required | el comando | el código del paso.")
    input: Any = Field(None, description=f"Solo con la sección `observability`. {_REDACTADO}")
    output: Any = Field(None, description=f"Solo con la sección `observability`. {_REDACTADO}")
    detalle_oculto: bool = Field(False, description="Hay entrada o salida pero falta `observability` para verla.")
    ok: bool | None = Field(None, description="¿Salió bien? Visible siempre; null si el paso no lo dice.")
    error_codes: list[str] = Field(default_factory=list, description="Códigos de error (sin valores).")


class TurnTaskEvent(BaseModel):
    at: datetime
    task_id: uuid.UUID
    procedure_id: str | None
    seq: int
    type: str
    actor: str
    data: dict[str, Any] = Field(description="Los datos de pago siempre van enmascarados.")


class TurnA2AEvent(BaseModel):
    seq: int
    type: str
    mode: str | None
    intent: str | None
    from_instance_id: uuid.UUID | None
    to_instance_id: uuid.UUID | None
    payload: Any = Field(None, description="Solo la forma (redactado al escribir, HU-AOS-18).")
    result: Any = None
    reason: str | None
    cost_usd: float | None = Field(None, description="Solo con la sección `costs`.")


class TurnA2A(BaseModel):
    id: uuid.UUID
    status: str
    mode: str | None
    calls: int
    spent_usd: float | None = Field(None, description="Solo con la sección `costs`.")
    events: list[TurnA2AEvent]


class TurnDelivery(BaseModel):
    id: uuid.UUID
    at: datetime
    status: str | None
    text: str | None
    template_name: str | None
    error_reason: str | None


class TurnCost(BaseModel):
    kind: str = Field(description="turn | agent_run | classifier | composer | embedding | eval")
    model: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = Field(None, description="Solo con la sección `costs`.")


class TurnDetail(BaseModel):
    turn_id: uuid.UUID
    conversation_id: uuid.UUID | None
    connection_id: uuid.UUID | None
    at: datetime
    latency_ms: int | None
    release_id: uuid.UUID | None
    release_number: int | None
    lane: str | None = Field(description="stable | canary | pinned | legacy")
    inbound_message_ids: list[uuid.UUID]
    decision: TurnDecision
    status: str | None = Field(description="Resultado del turno: replied | neutral | escalated | silent | failed.")
    verifier: list[dict[str, Any]] = Field(description="Comprobaciones del verificador: `{kind, verdict, count}`.")
    verifier_retries: int
    steps: list[TurnStep]
    task_events: list[TurnTaskEvent]
    a2a: list[TurnA2A]
    delivery: list[TurnDelivery]
    cost: list[TurnCost]
    can_see_detail: bool = Field(description="¿Tiene la sección `observability`?")
    can_see_costs: bool = Field(description="¿Tiene la sección `costs`?")


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _puede(principal: Principal, seccion: str) -> bool:
    if principal.admin.role == "master":
        return True
    return await effective_access(principal.admin, seccion, principal.tenant_id) is not None


def _agente(a: svc.Agente | None) -> TurnAgent | None:
    return TurnAgent(instance_id=a.instance_id, name=a.nombre) if a else None


@router.get("/turns/{turn_id}", response_model=TurnDetail)
async def turn_detail(
    turn_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations"))],
) -> TurnDetail:
    tid = _tenant(principal)
    ver_detalle = await _puede(principal, "observability")
    ver_costos = await _puede(principal, "costs")
    async with get_session() as session:
        crudo = await svc.leer(session, tid, turn_id)
    if crudo is None:
        raise ApiError.not_found("No existe ese turno en este negocio.")
    t = svc.redactar(crudo, ver_detalle=ver_detalle, ver_costos=ver_costos)
    return TurnDetail(
        turn_id=t.turn_id,
        conversation_id=t.conversation_id,
        connection_id=t.connection_id,
        at=t.at,
        latency_ms=t.latencia_ms,
        release_id=t.release_id,
        release_number=t.release_number,
        lane=t.carril,
        inbound_message_ids=t.mensajes_entrantes,
        decision=TurnDecision(
            outcome=t.resultado,
            blocked_by=t.bloqueado_por,
            route_source=t.origen_ruta,
            rule_id=t.regla,
            candidates=t.candidatos,
            confidence=t.confianza,
            active_agent=_agente(t.agente_activo),
            final_agent=_agente(t.agente_final),
            shadow_stack=t.pila,
        ),
        status=t.estado_final,
        verifier=t.verificador,
        verifier_retries=t.reintentos_verificador,
        steps=[
            TurnStep(
                at=p.at,
                kind=p.tipo,  # type: ignore[arg-type]
                actor=p.actor,
                name=p.nombre,
                action=p.accion,
                input=p.entrada,
                output=p.salida,
                detalle_oculto=p.detalle_oculto,
                ok=p.ok,
                error_codes=p.codigos,
            )
            for p in t.pasos
        ],
        task_events=[
            TurnTaskEvent(
                at=e.at, task_id=e.task_id, procedure_id=e.procedure_id, seq=e.seq, type=e.tipo, actor=e.actor, data=e.data
            )
            for e in t.estado
        ],
        a2a=[
            TurnA2A(
                id=o.id,
                status=o.estado,
                mode=o.modo,
                calls=o.llamadas,
                spent_usd=o.gasto_usd,
                events=[
                    TurnA2AEvent(
                        seq=ev.seq, type=ev.tipo, mode=ev.modo, intent=ev.intencion, from_instance_id=ev.desde,
                        to_instance_id=ev.hacia, payload=ev.payload, result=ev.resultado, reason=ev.motivo,
                        cost_usd=ev.costo_usd,
                    )
                    for ev in o.eventos
                ],
            )
            for o in t.a2a
        ],
        delivery=[
            TurnDelivery(id=e.id, at=e.at, status=e.estado, text=e.texto, template_name=e.plantilla, error_reason=e.error)
            for e in t.envio
        ],
        cost=[
            TurnCost(
                kind=c.tipo, model=c.modelo, calls=c.llamados, input_tokens=c.tokens_entrada, output_tokens=c.tokens_salida,
                cost_usd=c.costo_usd,
            )
            for c in t.costo
        ],
        can_see_detail=t.puede_ver_detalle,
        can_see_costs=t.costos_visibles,
    )


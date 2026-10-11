"""Ledger del Coordinador: decisión y resultado de cada turno (HU-AOS-10, adenda de ADR-0112).

Un **turno** es lo que el Coordinador hace con lo que entró: bloquearlo por una política,
escalarlo o enrutarlo al hilo del agente. Deja dos filas append-only:

- `core_decisions` (la cabecera, `id = turn_id`) **al decidir**, antes de cualquier efecto del
  turno (respuesta fija, escalamiento, LLM). Las filas que el turno causa después
  (`usage_ledger`, `audit_log`, `messages`) llevan su `turn_id` por el default de columna de
  `core.shared.turno`, que solo se fija cuando la cabecera ya commiteó: sus FKs a
  `core_decisions` son diferidas al COMMIT, pero cada `get_session()` es su propia transacción.
- `turn_outcomes` al cerrar el turno, después de la entrega (`core.kernel.egress`).

Dos INSERT por turno y ninguna lectura: la conexión sale del objeto `conversation` que el
llamador ya tiene en memoria, los tokens y el costo del uso que ya se acumula para
`usage_ledger`, las latencias de `perf_counter`.

Best-effort mientras el runtime sea legacy: un fallo de escritura se loguea y el turno sigue
igual (nunca cambia una respuesta). Desde `AOS_PROCEDURES` el design pide fail-closed (§5.3);
eso llega con el motor de procedimientos.

Trazas: el turno abre el span `kernel.turn` como span actual, así el hilo del agente
(`turn`, `llm_call`), las acciones (`tool_execute`) y la salida (la tarea Celery
`outbound_send`, por `core.observability.celery_trazas`) comparten `trace_id`. Con OTel apagado
se adjunta un span no grabado cuyo `trace_id` es el `turn_id`: sin costo, y la correlación
(logs, `core_decisions.trace_id`, `traceparent` de Celery) existe igual.

`KERNEL_LEDGER_ENABLED=false` apaga todo esto (las columnas quedan en NULL).
"""

from __future__ import annotations

import asyncio
import functools
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from core.config import get_settings
from core.db.session import get_session
from core.observability.tracing import current_trace_id, get_tracer
from core.shared import turno as turno_vigente
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Valores de los CHECK de 0069 (`core_decisions.blocked_by`, `turn_outcomes.status`).
BLOQUEOS = frozenset(
    {
        "kill_switch",
        "connection_paused",
        "quota",
        "budget",
        "out_of_hours",
        "escalated_conversation",
        "escalation_keyword",
    }
)
ESTADOS = frozenset({"replied", "neutral", "escalated", "silent", "failed"})
# Tope de cada escritura del ledger: best-effort también en latencia (pool agotado o locks
# no deben llevar el turno a `turn_timeout_s`). Un timeout cuenta como fallo de escritura.
_TIMEOUT_ESCRITURA_S = 2.0


@dataclass
class Turno:
    """Estado en memoria de un turno. Se escribe en dos filas; no se lee de la BD."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    t0: float
    trace_id: str | None = None
    conversation_id: uuid.UUID | None = None
    inbound_message_ids: list[uuid.UUID] = field(default_factory=list)
    decidido: bool = False
    outcome: str | None = None
    blocked_by: str | None = None
    status: str | None = None
    neutral: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    verifier: list[dict[str, Any]] = field(default_factory=list)
    verifier_retries: int = 0
    # Release del turno (HU-AOS-14): NULL con `aos_runtime = 'legacy'`.
    release_id: uuid.UUID | None = None
    release_lane: str | None = None
    _token_vigente: Any = None

    def ms(self) -> int:
        return max(0, int((time.perf_counter() - self.t0) * 1000))


_turno: ContextVar[Turno | None] = ContextVar("kernel_turno", default=None)


def turno_actual() -> Turno | None:
    """Turno abierto, solo si es del negocio en `tenant_scope`. Así un turno de otro negocio
    anidado (hoy no hay ninguno) abre el suyo y nunca anota tokens, veredicto ni status en
    el turno ajeno."""
    turno = _turno.get()
    if turno is None:
        return None
    actual = get_current_tenant_id()
    return turno if actual is not None and actual == turno.tenant_id else None


def _habilitado() -> bool:
    return bool(get_settings().kernel_ledger_enabled)


# ------------------------------------------------------------------ ciclo del turno
def en_turno[**P, R](
    fn: Callable[P, Awaitable[R]] | None = None, *, anota_resultado: bool = False
) -> Any:
    """Decorador: la función corre dentro de un turno del kernel. Reentrante: si ya hay un
    turno abierto (el consumer abre uno y llama al loop), se usa ese y no se cierra aquí.

    `anota_resultado`: la función devuelve el `TurnResult` del loop; su forma fija el status
    del turno (`replied`, `escalated`, `neutral` o `silent`)."""

    def decorar(f: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(f)
        async def envoltorio(*args: P.args, **kwargs: P.kwargs) -> R:
            if turno_actual() is not None or not _habilitado() or get_current_tenant_id() is None:
                resultado = await f(*args, **kwargs)
                if anota_resultado:
                    anotar_resultado(resultado)
                return resultado
            async with _abrir_turno():
                resultado = await f(*args, **kwargs)
                if anota_resultado:
                    anotar_resultado(resultado)
                return resultado

        return envoltorio

    return decorar(fn) if fn is not None else decorar


@asynccontextmanager
async def _abrir_turno() -> AsyncIterator[Turno]:
    tenant_id = get_current_tenant_id()
    assert tenant_id is not None
    turno = Turno(id=uuid.uuid4(), tenant_id=tenant_id, t0=time.perf_counter())
    token = _turno.set(turno)
    tracer = get_tracer()
    try:
        # Sin registrar la excepción en el span: su mensaje puede repetir datos del cliente
        # (parámetros de SQLAlchemy) y saldría al exportador. Solo el tipo.
        with tracer.start_as_current_span(
            "kernel.turn", record_exception=False, set_status_on_exception=False
        ) as span:
            respaldo = (
                nullcontext()
                if span.get_span_context().is_valid
                else trace.use_span(_span_de_respaldo(turno.id), end_on_exit=False)
            )
            with respaldo:
                turno.trace_id = current_trace_id()
                span.set_attribute("turn_id", str(turno.id))
                span.set_attribute("tenant_id", str(tenant_id))
                with structlog.contextvars.bound_contextvars(
                    turn_id=str(turno.id), trace_id=turno.trace_id
                ):
                    try:
                        yield turno
                    except Exception as exc:
                        # Solo `Exception`: una tarea cancelada no espera a la BD.
                        span.set_attribute("error.type", type(exc).__name__)
                        turno.status = "failed"
                        await _registrar_resultado(turno)
                        raise
                    await _registrar_resultado(turno)
                    span.set_attribute("outcome", turno.outcome or "")
                    span.set_attribute("status", turno.status or "")
    finally:
        # Blindado: si algún día se decide en otra task (otro contexto), `reset` lanzaría
        # `ValueError`; eso no debe tapar el resultado del turno ni dejarlo pegado.
        try:
            if turno._token_vigente is not None:
                turno_vigente.soltar(turno._token_vigente)
        except ValueError:
            log.warning("turno_vigente_reset_fuera_de_contexto")
        finally:
            try:
                _turno.reset(token)
            except ValueError:
                log.warning("kernel_turno_reset_fuera_de_contexto")


def _span_de_respaldo(turn_id: uuid.UUID) -> NonRecordingSpan:
    """Span no grabado con `trace_id = turn_id` (OTel apagado): el tracer no-op lo hereda en
    los spans hijos y el propagador W3C lo manda a Celery en `traceparent`."""
    return NonRecordingSpan(
        SpanContext(
            trace_id=turn_id.int,
            span_id=secrets.randbits(64) or 1,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.DEFAULT),
        )
    )


# ------------------------------------------------------------------ decisión
async def decidir(
    conversation: Any,
    outcome: str,
    *,
    blocked_by: str | None = None,
    route_source: str | None = None,
    status: str | None = None,
) -> None:
    """Escribe la cabecera del turno (`core_decisions`). La primera decisión gana: llamadas
    posteriores en el mismo turno no hacen nada. Sin turno abierto, no hace nada."""
    turno = turno_actual()
    if turno is None or turno.decidido or conversation is None:
        return
    if str(getattr(conversation, "tenant_id", None)) != str(turno.tenant_id):
        return
    turno.decidido = True
    turno.outcome = outcome
    turno.blocked_by = blocked_by
    turno.conversation_id = conversation.id
    if status is not None:
        turno.status = status
    try:
        async with asyncio.timeout(_TIMEOUT_ESCRITURA_S), get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO core_decisions (id, tenant_id, connection_id, conversation_id, "
                    "inbound_message_ids, outcome, blocked_by, route_source, latency_ms, trace_id, "
                    "release_id, release_lane) "
                    "VALUES (:id, :tenant_id, :connection_id, :conversation_id, :inbound, :outcome, "
                    ":blocked_by, :route_source, :latency_ms, :trace_id, :release_id, :release_lane)"
                ).bindparams(bindparam("inbound", type_=ARRAY(UUID(as_uuid=True)))),
                {
                    "id": turno.id,
                    "tenant_id": turno.tenant_id,
                    # Solo la conexión canónica (`channel_connections`); la de Embedded Signup
                    # queda NULL hasta el backfill de HU-AOS-11 (válido según el DDL).
                    "connection_id": getattr(conversation, "connection_id", None),
                    "conversation_id": conversation.id,
                    "inbound": list(turno.inbound_message_ids),
                    "outcome": outcome,
                    "blocked_by": blocked_by,
                    "route_source": route_source,
                    "latency_ms": turno.ms(),
                    "trace_id": turno.trace_id,
                    "release_id": turno.release_id,
                    "release_lane": turno.release_lane,
                },
            )
    except Exception:
        log.warning(
            "core_decision_write_failed", outcome=outcome, blocked_by=blocked_by, exc_info=True
        )
        return
    # Solo con la cabecera commiteada se propaga el `turn_id` (FKs diferidas al COMMIT).
    turno._token_vigente = turno_vigente.fijar(
        turno_vigente.TurnoVigente(
            turn_id=turno.id, tenant_id=turno.tenant_id, trace_id=turno.trace_id
        )
    )


async def enrutar(conversation: Any) -> None:
    """El turno va al hilo del agente. Con el runtime legacy no hay router: `default`."""
    await decidir(conversation, "routed", route_source="default")


async def bloquear(conversation: Any, motivo: str, *, status: str | None = None) -> None:
    """El turno se corta por una política, sin LLM."""
    assert motivo in BLOQUEOS, motivo
    outcome = "escalated" if motivo == "escalation_keyword" else "blocked"
    await decidir(conversation, outcome, blocked_by=motivo, status=status)


# ------------------------------------------------------------------ acumuladores del resultado
def anotar_release(release_id: uuid.UUID, lane: str) -> None:
    """Release y carril del turno (HU-AOS-14), antes de decidir: `core_decisions.release_id`."""
    turno = turno_actual()
    if turno is not None and not turno.decidido:
        turno.release_id = release_id
        turno.release_lane = lane


def anotar_entrada(message_id: uuid.UUID | None) -> None:
    """Id del mensaje entrante que abrió el turno (`core_decisions.inbound_message_ids`)."""
    turno = turno_actual()
    if turno is not None and isinstance(message_id, uuid.UUID) and not turno.decidido:
        turno.inbound_message_ids.append(message_id)


def anotar_uso(input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    turno = turno_actual()
    if turno is not None:
        turno.tokens_in += max(0, int(input_tokens))
        turno.tokens_out += max(0, int(output_tokens))
        turno.cost_usd += max(0.0, float(cost_usd))


def anotar_veredicto(verifier: list[dict[str, Any]], reintentos: int) -> None:
    turno = turno_actual()
    if turno is not None:
        turno.verifier = verifier
        turno.verifier_retries = max(0, reintentos)


def marcar_neutral() -> None:
    """Respuesta neutral del guardrail (ADR-0092)."""
    turno = turno_actual()
    if turno is not None:
        turno.neutral = True


def marcar_escalado() -> None:
    turno = turno_actual()
    if turno is not None:
        turno.status = "escalated"


def anotar_resultado(resultado: Any) -> None:
    """Status del turno desde el `TurnResult` del loop (`None` = no respondió)."""
    turno = turno_actual()
    if turno is None:
        return
    if resultado is None:
        status = "silent"
    elif getattr(resultado, "escalated", False):
        status = "escalated"
    elif turno.neutral:
        status = "neutral"
    elif getattr(resultado, "text", ""):
        status = "replied"
    else:
        status = "silent"
    turno.status = status


async def _registrar_resultado(turno: Turno) -> None:
    if not turno.decidido or turno._token_vigente is None:
        return  # sin cabecera no hay resultado (la FK no es diferida)
    status = turno.status if turno.status in ESTADOS else "silent"
    turno.status = status
    try:
        async with asyncio.timeout(_TIMEOUT_ESCRITURA_S), get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO turn_outcomes (turn_id, tenant_id, status, verifier, verifier_retries, "
                    "tokens_in, tokens_out, cost_usd, latency_ms) VALUES (:turn_id, :tenant_id, :status, "
                    "CAST(:verifier AS jsonb), :retries, :tokens_in, :tokens_out, :cost_usd, :latency_ms)"
                ),
                {
                    "turn_id": turno.id,
                    "tenant_id": turno.tenant_id,
                    "status": status,
                    "verifier": json.dumps(turno.verifier),
                    "retries": min(turno.verifier_retries, 32767),
                    "tokens_in": turno.tokens_in,
                    "tokens_out": turno.tokens_out,
                    "cost_usd": round(turno.cost_usd, 6),
                    "latency_ms": turno.ms(),
                },
            )
    except Exception:
        log.warning("turn_outcome_write_failed", status=status, exc_info=True)

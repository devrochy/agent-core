"""Propagación de la traza del turno a las tareas Celery (HU-AOS-10).

- Al publicar (`before_task_publish`, en el proceso que encola): el propagador W3C de
  `opentelemetry-api` escribe `traceparent` en los headers del mensaje, desde el span actual
  (el `kernel.turn` del turno, o su span de respaldo con `trace_id = turn_id` si OTel está
  apagado).
- En el worker (`task_prerun`/`task_postrun`): se extrae ese contexto de `task.request` y la
  tarea corre dentro de un span hijo (`celery.<tarea>`). Así la salida (`outbound_send`)
  comparte `trace_id` con el turno. El token del contexto se guarda por `task_id` y se suelta
  siempre en `task_postrun`.

Turno (seguimiento #439 de HU-AOS-10): el `turn_id` del turno viaja aparte, en los headers
`agentcore_turn_id`, `agentcore_turn_tenant_id` y `agentcore_turn_trace_id`, solo si al encolar
hay turno vigente del negocio en `tenant_scope` (la guarda del default de columna) y no es un
turno heredado. En el worker se fija en `core.shared.turno` al empezar cada tarea (o se limpia,
si no viene) y se suelta al terminar; `run_async` lo lleva a la corrutina, donde los defaults
de columna aplican la guarda de negocio por fila. El reintento (`self.retry`) conserva los
headers del mensaje; una tarea nueva encolada desde el worker no los hereda (NULL antes que
mal atribuido), aunque sí comparta `trace_id` por `traceparent`.

Sin dependencias nuevas. El `TracerProvider` sigue siendo perezoso (`get_tracer`): se
configura en el proceso hijo del prefork, no en el padre.
"""

from __future__ import annotations

import re
import uuid
from contextvars import Token
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from core.observability.tracing import get_tracer
from core.shared import turno as turno_vigente
from core.shared.logger import get_logger

log = get_logger(__name__)

# task_id -> (span, token del contexto adjuntado). Acotado: si un `task_postrun` no llega
# (worker matado), la entrada no se queda para siempre.
_activos: dict[str, tuple[trace.Span, object]] = {}
_MAX_ACTIVOS = 1024
# Solo W3C TraceContext, explícito: aunque alguien fije baggage (con datos del negocio), no
# viaja por el broker.
_propagador = TraceContextTextMapPropagator()
_HEADERS_W3C = ("traceparent", "tracestate")


class _GetterDeRequest(Getter[Any]):
    """Lee los headers W3C de `task.request` (Celery copia los headers del mensaje ahí)."""

    def get(self, carrier: Any, key: str) -> list[str] | None:
        valor = getattr(carrier, key, None)
        if valor is None and isinstance(getattr(carrier, "headers", None), dict):
            valor = carrier.headers.get(key)
        if valor is None:
            return None
        return [valor] if isinstance(valor, str) else list(valor)

    def keys(self, carrier: Any) -> list[str]:
        return list(_HEADERS_W3C)


_getter = _GetterDeRequest()


def al_publicar(headers: dict[str, Any] | None = None, **_kwargs: Any) -> None:
    if headers is None:
        return
    try:
        _propagador.inject(headers)
    except Exception:  # la traza nunca impide encolar
        return


def al_empezar(task_id: str | None = None, task: Any = None, **_kwargs: Any) -> None:
    if task_id is None or task is None:
        return
    try:
        padre = _propagador.extract(getattr(task, "request", None), getter=_getter)
        span = get_tracer().start_span(f"celery.{getattr(task, 'name', 'task')}", context=padre)
        token = otel_context.attach(trace.set_span_in_context(span, padre))
        if len(_activos) >= _MAX_ACTIVOS:
            _activos.clear()  # entradas huérfanas: solo higiene de memoria
        _activos[task_id] = (span, token)
    except Exception:
        return


def al_terminar(task_id: str | None = None, **_kwargs: Any) -> None:
    if task_id is None:
        return
    activo = _activos.pop(task_id, None)
    if activo is None:
        return
    span, token = activo
    try:
        otel_context.detach(token)  # type: ignore[arg-type]
    finally:
        span.end()


# ------------------------------------------------------------------ turno (#439)
H_TURNO = "agentcore_turn_id"
H_NEGOCIO = "agentcore_turn_tenant_id"
H_TRAZA = "agentcore_turn_trace_id"
_HEADERS_TURNO = (H_TURNO, H_NEGOCIO, H_TRAZA)
_TRAZA_RE = re.compile(r"[0-9a-f]{32}")
# task_id -> token del turno fijado al empezar. Acotado igual que `_activos`.
_turnos: dict[str, Token[turno_vigente.TurnoVigente | None]] = {}


def turno_al_publicar(headers: dict[str, Any] | None = None, **_kwargs: Any) -> None:
    """Escribe el turno vigente en los headers del mensaje. Nunca pisa headers de turno que
    ya vengan (reintento de una tarea) y nunca impide encolar."""
    if headers is None:
        return
    try:
        if any(h in headers for h in _HEADERS_TURNO):
            return
        turno = turno_vigente.vigente()  # guarda de negocio: solo bajo el tenant_scope del turno
        if turno is None or turno.heredado:
            return
        headers[H_TURNO] = str(turno.turn_id)
        headers[H_NEGOCIO] = str(turno.tenant_id)
        if turno.trace_id:
            headers[H_TRAZA] = turno.trace_id
    except Exception as exc:  # la atribución nunca impide encolar
        log.warning("celery_turno_publicar_fallo", error_type=type(exc).__name__)


def _primero(request: Any, clave: str) -> str | None:
    valores = _getter.get(request, clave) if request is not None else None
    return valores[0] if valores else None


def _turno_de(request: Any) -> turno_vigente.TurnoVigente | None:
    """Turno de los headers de la tarea, validado; None si falta o no es válido."""
    try:
        turn_id, tenant_id = _primero(request, H_TURNO), _primero(request, H_NEGOCIO)
        if not turn_id or not tenant_id:
            return None
        traza = _primero(request, H_TRAZA)
        return turno_vigente.TurnoVigente(
            turn_id=uuid.UUID(turn_id),
            tenant_id=uuid.UUID(tenant_id),
            trace_id=traza if traza and _TRAZA_RE.fullmatch(traza) else None,
            heredado=True,
        )
    except (TypeError, ValueError, AttributeError):
        return None


def turno_al_empezar(task_id: str | None = None, task: Any = None, **_kwargs: Any) -> None:
    """Fija SIEMPRE el turno de la tarea (o None): un `task_postrun` perdido nunca deja el
    turno de una tarea pegado a la siguiente del mismo hilo."""
    if task_id is None or task is None:
        return
    try:
        token = turno_vigente.fijar(_turno_de(getattr(task, "request", None)))
        if len(_turnos) >= _MAX_ACTIVOS:
            # Entradas huérfanas: solo higiene de memoria. Con pool de hilos, una tarea en
            # curso cuyo token se descarte deja su turno en el hilo hasta el próximo prerun,
            # que lo pisa siempre: no llega a otra tarea.
            _turnos.clear()
        _turnos[task_id] = token
    except Exception as exc:
        log.warning("celery_turno_empezar_fallo", error_type=type(exc).__name__)


def turno_al_terminar(task_id: str | None = None, **_kwargs: Any) -> None:
    if task_id is None:
        return
    token = _turnos.pop(task_id, None)
    if token is None:
        return
    try:
        turno_vigente.soltar(token)
    except (ValueError, RuntimeError):
        turno_vigente.fijar(None)  # otro contexto o token usado: no queda el turno pegado


def conectar() -> None:
    """Conecta las señales (lo llama `core.tasks.celery_app` al importarse)."""
    from celery.signals import before_task_publish, task_postrun, task_prerun

    before_task_publish.connect(al_publicar, weak=False)
    task_prerun.connect(al_empezar, weak=False)
    task_postrun.connect(al_terminar, weak=False)
    before_task_publish.connect(turno_al_publicar, weak=False)
    task_prerun.connect(turno_al_empezar, weak=False)
    task_postrun.connect(turno_al_terminar, weak=False)

"""Propagación de la traza del turno a las tareas Celery (HU-AOS-10).

- Al publicar (`before_task_publish`, en el proceso que encola): el propagador W3C de
  `opentelemetry-api` escribe `traceparent` en los headers del mensaje, desde el span actual
  (el `kernel.turn` del turno, o su span de respaldo con `trace_id = turn_id` si OTel está
  apagado).
- En el worker (`task_prerun`/`task_postrun`): se extrae ese contexto de `task.request` y la
  tarea corre dentro de un span hijo (`celery.<tarea>`). Así la salida (`outbound_send`)
  comparte `trace_id` con el turno. El token del contexto se guarda por `task_id` y se suelta
  siempre en `task_postrun`.

Sin dependencias nuevas. El `TracerProvider` sigue siendo perezoso (`get_tracer`): se
configura en el proceso hijo del prefork, no en el padre.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from core.observability.tracing import get_tracer

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


def conectar() -> None:
    """Conecta las señales (lo llama `core.tasks.celery_app` al importarse)."""
    from celery.signals import before_task_publish, task_postrun, task_prerun

    before_task_publish.connect(al_publicar, weak=False)
    task_prerun.connect(al_empezar, weak=False)
    task_postrun.connect(al_terminar, weak=False)

"""Métricas propias de la plataforma (ADR-0188): eventos que el código registra cuando pasan.

Usan la API de OpenTelemetry: sin `MeterProvider` (OTEL_METRICS_ENABLED=false) son no-op. El
proveedor lo fija `core.observability.metricas.configure_metrics()`.

| Métrica | Etiquetas | Dónde |
|---|---|---|
| `agentcore_llm_tokens_total` | `model`, `kind` (input, output, cache_read, cache_write) | respuesta del LLM en el loop |
| `agentcore_llm_cost_usd_total` | `model` | idem (USD estimados por el proveedor) |
| `agentcore_llm_calls_total` | `model`, `outcome` (ok, error) | idem, y el fallo del proveedor |
| `agentcore_turn_duration_seconds` | `channel`, `outcome` (status del turno o `sin_decision`) | cierre del turno del Coordinador |
| `agentcore_celery_tasks_total` | `task`, `status` (success, failure, retry…) | `task_postrun` |
| `agentcore_celery_task_duration_seconds` | — (sin `task`: cada tarea multiplicaría los buckets) | `task_postrun` |
| `agentcore_graph_api_errors_total` | `category` (rate_limit, auth, transient…) | `graph_fetch` |
| `agentcore_http_server_duration_seconds` | `route_group` (webhooks, api, admin, other), `status_class` | middleware de la API |

**Etiquetas prohibidas** (cada valor es una serie y el plan gratis da 10k): `tenant_id`,
`agent_id`, `conversation_id`, teléfonos, correos y rutas sin agrupar. Las etiquetas son conjuntos
cerrados del código; `_etiqueta` lo garantiza aunque llegue un valor raro (pasa a `otro`). Los
histogramas llevan buckets explícitos y cortos por la misma razón.

Registrar una métrica nunca rompe el camino que la registra.
"""

from __future__ import annotations

import re
import time
from typing import Any

from opentelemetry import metrics

from core.shared.logger import get_logger

log = get_logger(__name__)

OTRO = "otro"
_PERMITIDO = re.compile(r"[a-z0-9_.:/-]{1,64}")

_meter = metrics.get_meter("agent-core")

_tokens = _meter.create_counter("agentcore_llm_tokens_total", description="Tokens del LLM por modelo y tipo")
_costo = _meter.create_counter("agentcore_llm_cost_usd_total", description="Costo estimado del LLM en USD por modelo")
_llamadas = _meter.create_counter("agentcore_llm_calls_total", description="Llamadas al LLM por modelo y resultado")
_turnos = _meter.create_histogram(
    "agentcore_turn_duration_seconds",
    unit="s",
    description="Duración del turno del Coordinador por canal y resultado",
    explicit_bucket_boundaries_advisory=[0.5, 1, 2, 5, 10, 20, 30, 60, 120],
)
_tareas = _meter.create_counter("agentcore_celery_tasks_total", description="Tareas de Celery terminadas por nombre y estado")
_duracion_tareas = _meter.create_histogram(
    "agentcore_celery_task_duration_seconds",
    unit="s",
    description="Duración de las tareas de Celery",
    explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300],
)
_errores_graph = _meter.create_counter(
    "agentcore_graph_api_errors_total", description="Errores de Graph API por categoría"
)
_http = _meter.create_histogram(
    "agentcore_http_server_duration_seconds",
    unit="s",
    description="Duración de las peticiones HTTP de la API por grupo de ruta y clase de estado",
    explicit_bucket_boundaries_advisory=[0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)


def _etiqueta(valor: object) -> str:
    """Valor de una etiqueta: minúsculas de un conjunto cerrado; cualquier otra cosa es `otro`."""
    texto = str(valor or "").strip().lower()
    return texto if _PERMITIDO.fullmatch(texto) else OTRO


def _seguro(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception as exc:  # una métrica nunca rompe el turno, la tarea ni la petición
        log.warning("metrica_fallo", error_type=type(exc).__name__)


# ------------------------------------------------------------------------------------- LLM
def llm_respuesta(
    modelo: str, *, entrada: int, salida: int, cache_leido: int, cache_escrito: int, costo_usd: float
) -> None:
    """Una respuesta del LLM. `entrada` es el total de entrada (lo cacheado incluido)."""

    def _registrar() -> None:
        m = _etiqueta(modelo)
        for tipo, valor in (("input", entrada), ("output", salida), ("cache_read", cache_leido), ("cache_write", cache_escrito)):
            if valor > 0:
                _tokens.add(int(valor), {"model": m, "kind": tipo})
        if costo_usd > 0:
            _costo.add(float(costo_usd), {"model": m})
        _llamadas.add(1, {"model": m, "outcome": "ok"})

    _seguro(_registrar)


def llm_fallo(modelo: str) -> None:
    _seguro(lambda: _llamadas.add(1, {"model": _etiqueta(modelo), "outcome": "error"}))


# ------------------------------------------------------------------------------------ turno
def turno(canal: str | None, outcome: str | None, segundos: float) -> None:
    _seguro(
        lambda: _turnos.record(max(0.0, segundos), {"channel": _etiqueta(canal), "outcome": _etiqueta(outcome)})
    )


# ----------------------------------------------------------------------------------- Celery
_inicios: dict[str, float] = {}
_MAX_INICIOS = 1024


def tarea_empezo(task_id: str | None = None, **_kwargs: Any) -> None:
    if task_id is None:
        return
    if len(_inicios) >= _MAX_INICIOS:
        _inicios.clear()  # entradas huérfanas (worker matado): solo higiene de memoria
    _inicios[task_id] = time.monotonic()


def tarea_termino(task_id: str | None = None, task: Any = None, state: str | None = None, **_kwargs: Any) -> None:
    inicio = _inicios.pop(task_id, None) if task_id is not None else None

    def _registrar() -> None:
        _tareas.add(1, {"task": _etiqueta(getattr(task, "name", None)), "status": _etiqueta(state)})
        if inicio is not None:
            _duracion_tareas.record(time.monotonic() - inicio)

    _seguro(_registrar)


# ------------------------------------------------------------------------------------ Graph
def error_graph(categoria: str | None) -> None:
    _seguro(lambda: _errores_graph.add(1, {"category": _etiqueta(categoria)}))


# ------------------------------------------------------------------------------------- HTTP
def grupo_de_ruta(path: str) -> str | None:
    """`route_group` de una ruta; `None` = no se mide (`/healthz*`, sondas de Coolify)."""
    if path.startswith("/healthz"):
        return None
    if path.startswith("/webhooks"):
        return "webhooks"
    if path.startswith("/api"):
        return "api"
    if path.startswith("/admin"):
        return "admin"
    return "other"


class MetricasHttpMiddleware:
    """Duración por `route_group` y clase de estado (2xx…5xx). ASGI puro."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        grupo = grupo_de_ruta(scope.get("path", "")) if scope.get("type") == "http" else None
        if grupo is None:
            await self.app(scope, receive, send)
            return
        inicio = time.monotonic()
        estado = {"codigo": 500}  # si la app revienta antes de responder, cuenta como 5xx

        async def _send(mensaje: dict[str, Any]) -> None:
            if mensaje.get("type") == "http.response.start":
                estado["codigo"] = int(mensaje.get("status", 500))
            await send(mensaje)

        try:
            await self.app(scope, receive, _send)
        finally:
            clase = f"{min(max(estado['codigo'] // 100, 1), 5)}xx"
            _seguro(
                lambda: _http.record(time.monotonic() - inicio, {"route_group": grupo, "status_class": clase})
            )

"""Trazas OpenTelemetry (Sprint 2).

Span por turno con hijos `llm_call`/`tool_execute`. Exportación OTLP configurable
por env; sin `OTEL_ENABLED` el tracer por defecto es no-op (cero overhead).
`trace_id` queda disponible para el logger y el panel.

Autoinstrumentación (ADR-0188), solo con `OTEL_ENABLED`:
- FastAPI: span por petición, sin `/healthz*` y sin los spans internos de send/receive. La
  query sale de `http.url`/`url.query` (el GET de verificación del webhook trae `hub.verify_token`).
- SQLAlchemy: `db.statement` es el SQL del cursor, con marcadores (`$1`), nunca los valores.
- httpx: la URL del span va sin query (`/oauth/access_token?client_secret=…`, tokens de Meta).
- Las tres con un `MeterProvider` no-op: sus histogramas llevan la ruta, el host y el código, y
  se comerían el presupuesto de series; las métricas HTTP propias van agrupadas.
- Celery no se instrumenta (`celery_trazas.py` ya propaga el contexto) ni Redis.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from core.config import get_settings

_tracer: trace.Tracer | None = None
_configured = False


def _configure() -> None:
    global _tracer, _configured
    if _configured:
        return
    _configured = True
    settings = get_settings()
    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint))
        )
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("agent-core")
    instrumentar_httpx()


def get_tracer() -> trace.Tracer:
    if get_settings().otel_enabled:
        _configure()
        return _tracer or trace.get_tracer("agent-core")
    # proveedor por defecto = no-op: spans sin costo cuando OTel está apagado
    return trace.get_tracer("agent-core")


def current_trace_id() -> str | None:
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is not None and ctx.trace_id != 0:
        return format(ctx.trace_id, "032x")
    return None


def span_attributes(span: trace.Span, **attrs: object) -> None:
    for key, value in attrs.items():
        span.set_attribute(key, str(value))


async def record_llm_call(model: str, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    """Registra el costo del turno en un atributo del span actual (si hay span)."""
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("llm.cost_usd", str(cost_usd))


def conversation_key(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    return f"{tenant_id}:{conversation_id}"

# ------------------------------------------------------------ autoinstrumentación (ADR-0188)
EXCLUIDAS = "/healthz"  # regex contra la URL completa: `/healthz` y `/healthz/deep`
_QUERY_TAPADA = "[redactado]"
_ATRIBUTOS_URL = ("http.url", "url.full")
_httpx_instrumentado = False


def sin_query(url: str) -> str:
    """La URL sin query ni fragmento (ni usuario:clave)."""
    try:
        partes = urlsplit(url)
    except ValueError:
        return ""
    netloc = partes.hostname or ""
    if partes.port:
        netloc = f"{netloc}:{partes.port}"
    return urlunsplit((partes.scheme, netloc, partes.path, "", ""))


def _tapar_url(span: Any) -> None:
    if span is None or not span.is_recording():
        return
    atributos = getattr(span, "attributes", None) or {}
    for clave in _ATRIBUTOS_URL:
        valor = atributos.get(clave)
        if isinstance(valor, str):
            span.set_attribute(clave, sin_query(valor))
    if atributos.get("url.query"):
        span.set_attribute("url.query", _QUERY_TAPADA)


def _hook_servidor(span: Any, _scope: dict[str, Any]) -> None:
    _tapar_url(span)


def _hook_httpx(span: Any, _request: Any) -> None:
    _tapar_url(span)


async def _hook_httpx_async(span: Any, _request: Any) -> None:
    _tapar_url(span)


def instrumentar_app(app: Any, tracer_provider: Any = None) -> None:
    """Spans de FastAPI. Se llama al crear la app; sin OTEL_ENABLED, no-op."""
    if tracer_provider is None:
        if not get_settings().otel_enabled:
            return
        _configure()
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=NoOpMeterProvider(),
        excluded_urls=EXCLUIDAS,
        exclude_spans=["receive", "send"],
        server_request_hook=_hook_servidor,
    )


def instrumentar_engine(engine: Any, tracer_provider: Any = None) -> None:
    """Spans de SQLAlchemy para un engine (async o sync). Sin OTEL_ENABLED, no-op."""
    if tracer_provider is None:
        if not get_settings().otel_enabled:
            return
        _configure()
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(
        engine=getattr(engine, "sync_engine", engine),
        tracer_provider=tracer_provider,
        meter_provider=NoOpMeterProvider(),
        enable_commenter=False,
        # La 0.66 declara `sqlalchemy < 2.1` y la imagen instala 2.1: sin esto no instrumenta nada
        # (solo un log de error). Los eventos que usa (`before/after_cursor_execute`) no cambiaron
        # en 2.1; `tests/test_observabilidad_autoinstrumentacion.py` lo comprueba con un engine real.
        skip_dep_check=True,
    )


def instrumentar_httpx() -> None:
    """Spans de todos los clientes httpx del proceso, sin query en la URL. Una vez por proceso
    (lo llama `_configure`, así que en el worker corre en el hijo del prefork)."""
    global _httpx_instrumentado
    if _httpx_instrumentado:
        return
    _httpx_instrumentado = True
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument(
        meter_provider=NoOpMeterProvider(),
        request_hook=_hook_httpx,
        async_request_hook=_hook_httpx_async,
    )

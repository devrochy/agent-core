"""Trazas OpenTelemetry (Sprint 2).

Span por turno con hijos `llm_call`/`tool_execute`. Exportación OTLP configurable
por env; sin `OTEL_ENABLED` el tracer por defecto es no-op (cero overhead).
`trace_id` queda disponible para el logger y el panel.
"""

from __future__ import annotations

import uuid

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
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
"""Observabilidad (Sprint 2): trazas OpenTelemetry."""

from core.observability.tracing import current_trace_id, get_tracer

__all__ = ["current_trace_id", "get_tracer"]
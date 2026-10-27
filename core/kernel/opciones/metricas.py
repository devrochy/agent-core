"""Métricas del toque (SPEC-40, ADR-0172 §Observabilidad): `option_taps_total{outcome}`,
`option_tap_latency_seconds` y `option_tool_errors_total{tool}`.

Usan la API de OpenTelemetry (`opentelemetry.metrics`), que sin un `MeterProvider` configurado es un no-op
(igual que `core/observability/tracing.py` sin `OTEL_ENABLED`). Hoy el repo no configura el proveedor de
métricas ni expone `/metrics`: lo observable es el log estructurado `option_tap` (un evento por toque, con
`outcome` y `latency_ms`); los instrumentos quedan listos para cuando se conecte el exportador. Las
etiquetas son un conjunto cerrado (`outcome`) o el nombre de una tool del catálogo (`tool`): nunca ids ni
valores.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("agent-core")
_toques = _meter.create_counter("option_taps_total", description="Toques de opciones directas por resultado")
_latencia = _meter.create_histogram(
    "option_tap_latency_seconds", unit="s", description="Del inbound a la respuesta fija de un toque"
)
_errores_tool = _meter.create_counter(
    "option_tool_errors_total", description="Tools que fallaron al ejecutarse desde una opción"
)


def toque(outcome: str, segundos: float) -> None:
    _toques.add(1, {"outcome": outcome})
    _latencia.record(max(0.0, segundos), {"outcome": outcome})


def error_de_tool(tool: str) -> None:
    _errores_tool.add(1, {"tool": tool[:80]})

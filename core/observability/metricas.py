"""Proveedor de métricas OpenTelemetry (ADR-0188).

Los contadores del código (`core/kernel/opciones/metricas.py`, `core/gateway/avisos_*.py`…) se
declaran con la API (`opentelemetry.metrics.get_meter`), que es un proxy no-op hasta que alguien
fija un `MeterProvider`. `configure_metrics()` lo fija una vez por proceso, con un
`PeriodicExportingMetricReader` que manda OTLP/HTTP cada 60 s a Alloy (que reenvía a Grafana
Cloud: el backend no lleva credenciales). Los instrumentos ya creados por la API se enganchan
solos al proveedor nuevo.

- Apagado por defecto (`OTEL_METRICS_ENABLED=false`): no se importa el SDK de métricas.
- El SDK se importa tarde y con red: el Python del runner de CI no trae `_ctypes`, que el SDK
  de métricas necesita. Si no carga, el proceso arranca igual, sin métricas, con un warning.
- Temporalidad acumulada (la que espera Mimir/Prometheus), fija en código para que una
  variable `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` no la cambie.
- Recurso: `service.name` (OTEL_SERVICE_NAME), `service.instance.id = <host>-<pid>` y lo que
  traiga `OTEL_RESOURCE_ATTRIBUTES` (p. ej. `deployment.environment=test`), que el SDK mezcla.
- Una vez por proceso, y en el proceso que exporta: en el worker de Celery va en
  `worker_process_init` (el hijo del prefork), nunca en el padre, porque el hilo del lector no
  sobrevive al fork.
"""

from __future__ import annotations

import os
import socket
from typing import Any

from core.config import get_settings
from core.shared.logger import get_logger

log = get_logger(__name__)

INTERVALO_EXPORTACION_MS = 60_000

_proveedor: Any = None


def instancia() -> str:
    """`service.instance.id`: distingue los procesos del mismo servicio (hijos del prefork)."""
    return f"{socket.gethostname()}-{os.getpid()}"


def recurso() -> Any:
    from opentelemetry.sdk.resources import Resource

    # `Resource.create` mezcla OTEL_RESOURCE_ATTRIBUTES; los atributos explícitos ganan.
    return Resource.create(
        {"service.name": get_settings().otel_service_name, "service.instance.id": instancia()}
    )


def _lector_otlp(endpoint: str) -> Any:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import (
        Counter,
        Histogram,
        ObservableCounter,
        ObservableGauge,
        ObservableUpDownCounter,
        UpDownCounter,
    )
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
        PeriodicExportingMetricReader,
    )

    acumulada = AggregationTemporality.CUMULATIVE
    exportador = OTLPMetricExporter(
        # Explícito: sin `endpoint`, el exportador lee OTEL_EXPORTER_OTLP_ENDPOINT como base y le
        # agrega `/v1/metrics`; aquí esa variable es la URL completa de las trazas.
        endpoint=endpoint,
        preferred_temporality={
            Counter: acumulada,
            UpDownCounter: acumulada,
            Histogram: acumulada,
            ObservableCounter: acumulada,
            ObservableUpDownCounter: acumulada,
            ObservableGauge: acumulada,
        },
    )
    return PeriodicExportingMetricReader(exportador, export_interval_millis=INTERVALO_EXPORTACION_MS)


def configure_metrics(lector: Any = None) -> bool:
    """Fija el `MeterProvider` del proceso. Idempotente; `True` si quedó configurado.

    `lector` es para los tests (un `InMemoryMetricReader`); en servicio sale de la config.
    """
    global _proveedor
    if _proveedor is not None:
        return True
    settings = get_settings()
    if not settings.otel_metrics_enabled:
        return False
    if lector is None and not settings.otel_metrics_endpoint:
        log.warning("metricas_sin_endpoint", nota="OTEL_METRICS_ENABLED sin OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
        return False
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider

        proveedor = MeterProvider(
            resource=recurso(),
            metric_readers=[lector if lector is not None else _lector_otlp(settings.otel_metrics_endpoint)],
        )
    except ImportError as exc:  # p. ej. un Python sin `_ctypes`: el proceso sigue sin métricas
        log.warning("metricas_sdk_no_disponible", error_type=type(exc).__name__)
        return False
    metrics.set_meter_provider(proveedor)
    _proveedor = proveedor
    log.info("metricas_configuradas", instancia=instancia(), intervalo_s=INTERVALO_EXPORTACION_MS // 1000)
    return True


def shutdown_metrics() -> None:
    """Exporta lo pendiente y suelta el lector (al apagar el proceso). Sin proveedor, no-op.

    Sin esto, cada despliegue pierde hasta 60 s de contadores."""
    global _proveedor
    proveedor, _proveedor = _proveedor, None
    if proveedor is None:
        return
    try:
        proveedor.shutdown(timeout_millis=5_000)
    except Exception as exc:  # apagar nunca falla el apagado del proceso
        log.warning("metricas_shutdown_fallo", error_type=type(exc).__name__)

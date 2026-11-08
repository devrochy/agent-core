## Observabilidad B1: proveedor de métricas OTLP (ADR-0188) (2026-11-08)

- `core/observability/metricas.py`: `configure_metrics()` fija un `MeterProvider` con exportador OTLP/HTTP cada 60 s, temporalidad acumulada y recurso `service.name` / `service.instance.id = <host>-<pid>` / `OTEL_RESOURCE_ATTRIBUTES`. Se llama en el lifespan de la API, en cada hijo del prefork del worker (`worker_process_init`, no en el padre), en `beat_init` y en `main()` del conector; `shutdown_metrics()` exporta lo pendiente al salir.
- Con eso salen los contadores que ya existían (`option_*`, `event_notice_omitted_total`…).
- Variables nuevas: `OTEL_METRICS_ENABLED` (por defecto `false` = no-op, ni se importa el SDK) y `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` (URL completa, `http://alloy:4318/v1/metrics`).
- Si el SDK de métricas no carga (un Python sin `_ctypes`, como el del runner de CI), el proceso arranca igual con un warning. Las pruebas del SDK real se saltan en ese Python.

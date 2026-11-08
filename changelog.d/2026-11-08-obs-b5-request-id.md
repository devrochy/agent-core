## Observabilidad B5: `X-Request-ID` y `trace_id` en los logs fuera del turno (ADR-0188) (2026-11-08)

- Middleware ASGI `RequestIdMiddleware`: cada petición lleva un id (el del cliente si es seguro, `[A-Za-z0-9._-]{1,64}`, o uno nuevo), que vuelve en la cabecera `X-Request-ID` y va como `request_id` en todos los logs de la petición.
- Procesador `con_trace_id` en structlog: agrega el `trace_id` del span vigente cuando el evento no trae uno (dentro del turno manda el del Coordinador). Sirve para saltar de una línea de Loki/Kibana a la traza en Tempo desde peticiones HTTP y tareas de Celery.
- Los registros de `logging` (httpx, SQLAlchemy, Celery) también llevan `request_id` y `trace_id` (`foreign_pre_chain`).

# ADR-0188 — Observabilidad: métricas, trazas y logs hacia Grafana Cloud (y Kibana para logs)

- **Estado:** aceptado (2026-11-08, verificado en test con el backend `be29a373`: 18 métricas `agentcore_*` en Grafana, dependencias en 1, trazas `kernel.turn → llm_call` en Tempo y `/healthz/deep` en 200 por el puerto local. El plan lo aprobó el dueño el 2026-11-07. Sin probar: disparo real de «Worker sin latido», DLQ con `XADD`, Sentry del conector y todo lo de producción, que sigue en HU-ARCH-07 y HU-ARCH-32).
- **Fecha:** 2026-11-08
- **Contexto:** HU-ARCH-07 (observabilidad de producción, P0) y D-SPEC-01-2 («OTLP a un servicio gestionado gratis»). Decisiones del dueño del 2026-11-07: Grafana Cloud gratis para métricas, trazas, dashboards y alertas; Elasticsearch y Kibana autohospedados solo para logs; alertas por Slack; enlaces solo para master desde el panel.
- **Usa:** HU-AOS-10 (`kernel.turn`, `trace_id` y propagación a Celery en `core/observability/celery_trazas.py`), ADR-0183 (`cron_runs`), ADR-0120 (CI por niveles).
- **PRs del backend:** B1 #720 (proveedor de métricas), B4 #721 (`/healthz/deep`), B6 #722 (Sentry en el conector), B2 #724 (autoinstrumentación), B5 #725 (`X-Request-ID`), B3 #726 (métricas propias y sondas).

## Contexto

Antes de este ADR nada de la plataforma se veía ni alertaba desde fuera:

- Los logs JSON de structlog solo vivían en el `json-file` de Docker de cada contenedor.
- Las trazas OTel existían (`kernel.turn → llm_call/tool_execute`) pero estaban apagadas.
- Había contadores declarados con la API de OTel (`option_*`, `event_notice_omitted_total`), pero ningún `MeterProvider`: no salían del proceso.
- `/healthz` es estático y no ve la base, Redis ni el worker.
- Sentry solo estaba en api, worker y beat, y sin DSN en ningún entorno.

Todo corre en una sola máquina. El plan gratis de Grafana Cloud da **10k series activas**, 50 GB/mes de logs y 50 GB/mes de trazas, con 14 días de retención.

## Decisión

### 1. Un recolector en el host; el backend no lleva credenciales

Un Grafana Alloy (repo `solvershub-infra`, fuera de este ADR) se une a la red Docker `coolify` como `alloy`. Recibe OTLP/HTTP en `http://alloy:4318` y reenvía a Grafana Cloud: Mimir para métricas y Tempo para trazas. También lee los logs de los contenedores, los redacta y los manda a Loki y a Elasticsearch.

**Las credenciales de Grafana Cloud viven solo en Alloy.** El backend solo conoce la URL interna.

### 2. Métricas: eventos en el código, sondas solo en la API

**Proveedor** (`core/observability/metricas.py`, `configure_metrics()`):
- `MeterProvider` con `OTLPMetricExporter` (HTTP) y `PeriodicExportingMetricReader` cada 60 s.
- Temporalidad **acumulada** fija en código, porque es la que espera Mimir.
- Recurso: `service.name` (`OTEL_SERVICE_NAME`), `service.instance.id = <host>-<pid>` y `OTEL_RESOURCE_ATTRIBUTES` (p. ej. `deployment.environment=test`).

**Dónde se configura.** Una vez por proceso, en el proceso que exporta:

| Proceso | Punto de arranque | Exportación al salir |
|---|---|---|
| API | lifespan | `shutdown_metrics()` al cerrar |
| Worker | `worker_process_init`, en el **hijo** del prefork, nunca en el padre, porque el hilo del lector no sobrevive al fork | `worker_process_shutdown` |
| Beat | `beat_init` | `atexit` del SDK |
| Conector | `main()` | `finally` |

**Apagado por defecto.** Sin `OTEL_METRICS_ENABLED` no se importa ni el SDK de métricas, y los instrumentos de la API son no-op. Si el SDK no carga, el proceso arranca igual y deja un warning.

**Eventos** (`core/observability/metricas_negocio.py`). El código los registra donde ocurren:

| Métrica | Etiquetas |
|---|---|
| `agentcore_llm_tokens_total` | `model`, `kind` (input, output, cache_read, cache_write) |
| `agentcore_llm_cost_usd_total` | `model` |
| `agentcore_llm_calls_total` | `model`, `outcome` (ok, error) |
| `agentcore_turn_duration_seconds` | `channel`, `outcome` |
| `agentcore_celery_tasks_total` | `task`, `status` |
| `agentcore_celery_task_duration_seconds` | — |
| `agentcore_graph_api_errors_total` | `category` |
| `agentcore_http_server_duration_seconds` | `route_group` (webhooks, api, admin, other), `status_class` |

**Sondas** (`core/observability/sondas.py`). Corren **solo en el rol api** y con métricas encendidas: un solo proceso mide y no se repiten series.
- Una tarea asyncio mide cada 30 s y deja los valores en un dict.
- Los `ObservableGauge` solo leen ese dict. El callback del SDK corre en el hilo del lector y nunca toca la base ni Redis.
- **Grafana no se conecta a Postgres.**

| Gauge | Fuente |
|---|---|
| `agentcore_dlq_length` | entradas de la DLQ, sin las sembradas por los escenarios de prueba |
| `agentcore_inbound_pending` | pendientes y lag del grupo del orquestador |
| `agentcore_worker_heartbeat_age_seconds` | edad del latido; ausente = 9999 |
| `agentcore_dependency_up{component}` | db y redis |
| `agentcore_cron_failures_24h{job}` | `crons_service.fallos_24h`, el único que toca `cron_runs` |

### 3. Cardinalidad: etiquetas prohibidas y presupuesto

**Nunca son etiqueta de una métrica:** `tenant_id`, `agent_id`, `conversation_id`, teléfono, correo, la ruta HTTP sin agrupar ni ningún texto de un cliente. Esa información va en trazas y logs, nunca en series.

Cómo se hace cumplir:
- Las etiquetas son conjuntos cerrados del código. `_etiqueta()` convierte en `otro` cualquier valor que no sea `[a-z0-9_.:/-]{1,64}`.
- Los histogramas llevan buckets explícitos de 9 límites (`explicit_bucket_boundaries_advisory`).
- `agentcore_celery_task_duration_seconds` no lleva `task`, porque cada tarea multiplicaría los buckets.
- Las autoinstrumentaciones (FastAPI, SQLAlchemy y httpx) usan un `MeterProvider` no-op: sus histogramas llevan ruta, host y código. La duración HTTP sale agrupada por `route_group`.

**Presupuesto: objetivo ~3k series en total, alerta a 8k.** Estimación por entorno:

| Proceso | Series |
|---|---|
| API | ~700 (dominan los histogramas de turno y HTTP) |
| Worker | ~100 por hijo del prefork × 8 hijos, porque `service.instance.id` distingue cada hijo |

Test y producción juntos, más el host y cAdvisor de Alloy, pueden pasar de 3k. Se mide en la fase 2. Si se pasa, hay dos salidas:
- Agregar fuera `service.instance.id` en las métricas del worker: Adaptive Metrics de Grafana Cloud, o un procesador en Alloy.
- Bajar la concurrencia del worker de test.

### 4. Trazas: autoinstrumentación con redacción en origen

Con `OTEL_ENABLED=true`:
- **FastAPI.** Sin `/healthz*` y sin los spans internos de send/receive. La query se quita de `http.url` y se tapa en `url.query`, porque el GET de verificación del webhook trae `hub.verify_token`.
- **SQLAlchemy.** El engine del core. `db.statement` es el SQL del cursor con marcadores (`$1`), nunca los valores, y no lleva sqlcommenter. Se usa `skip_dep_check`: la instrumentación 0.66 declara `sqlalchemy < 2.1` y la imagen instala 2.1; los eventos de cursor no cambiaron.
- **httpx.** Todos los clientes del proceso, con la URL sin query (`client_secret`, `fb_exchange_token`) y sin usuario:clave.
- **No se instrumentan:**
  - **Celery**, porque `celery_trazas.py` ya propaga `traceparent`.
  - **Redis**, por volumen y porque las claves llevan ids.

**Errores y rutas con secreto.** Lo encontró el auditor multitenant antes del merge:
- `ExportadorRedactado` envuelve el exportador y deja de cada span solo el código del status y el tipo de las excepciones. El instrumentador de SQLAlchemy pone `str(exc)` en el status, y con asyncpg trae el `DETAIL` con valores de la fila. FastAPI registra el mensaje de cualquier excepción no manejada.
- El engine del core usa `hide_parameters=True`, así que ningún error de SQL lleva `[parameters: …]`.
- El token firmado de `GET /api/v1/files/{token}` se tapa en la ruta del span.

El span `kernel.turn` sigue sin registrar el mensaje de las excepciones, solo su tipo.

### 5. Logs: correlación en el backend, redacción en Alloy

**En el backend.** Cada petición lleva un `X-Request-ID`:
- Se acepta el del cliente si es `[A-Za-z0-9._-]{1,64}`; si no, se genera uno nuevo.
- Vuelve en la respuesta y va como `request_id` en todos sus logs.
- `con_trace_id` agrega el `trace_id` del span vigente fuera del turno (dentro del turno manda el del Coordinador).
- Los registros de `logging` (httpx, SQLAlchemy, Celery) también llevan los dos, vía `foreign_pre_chain`.

Así se salta de una línea en Loki o Kibana a la traza en Tempo.

**La redacción va en Alloy**, con pruebas en `solvershub-infra`, y antes de que nada salga de la máquina:
- Correos y teléfonos `\+?\d{10,15}`.
- JWT, `Bearer …`, tokens de Meta `EAA…`, `access_token=…` y `sk-…`.
- Además se descartan las líneas de `/healthz`.

La redacción del backend (sin query en spans, Sentry con scrubber, `httpx` sin logs de URL) es la primera línea. La de Alloy es la red de seguridad. En Loki las únicas etiquetas son `env`, `service` y `level`; `trace_id` va como metadato estructurado.

### 6. Salud profunda: `/healthz/deep`

`/healthz` no cambia: lo usan Coolify y compose, y no debe tumbar la API porque caiga Redis o el worker.

`/healthz/deep`:
- Mide `SELECT 1` y `PING`, con tope de 1 s cada uno, y la edad del latido del worker (falla con más de 180 s o sin latido).
- Responde 200 o 503 con `{status, componentes}`, solo con `ok`/`fallo`: la ruta es pública por el prefijo `/healthz`.
- Guarda el resultado 5 s, también el 503, y las peticiones simultáneas comparten una medición.

### 7. Sentry también en el conector

`main()` del conector llama a `configure_sentry()`, con el mismo scrubber que el resto, y marca sus eventos con `role=connector`. La denylist del scrubber suma las variables locales del conector que llevan el texto, el teléfono o el QR de vinculación: `event`, `job`, `qr`, `sender`, `jid`, `to`, `raw` y `text`.

## Variables de entorno

| Variable | Dónde | Valor | Nueva |
|---|---|---|---|
| `OTEL_METRICS_ENABLED` | api, worker, beat, conector | `true` (por defecto `false`) | sí |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | api, worker, beat, conector | `http://alloy:4318/v1/metrics`, URL completa | sí |
| `OTEL_ENABLED` | api, worker, beat, conector | `true` | no |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | api, worker, beat, conector | `http://alloy:4318/v1/traces`, URL completa | no |
| `OTEL_SERVICE_NAME` | cada rol | `agent-core-<rol>` | no |
| `OTEL_RESOURCE_ATTRIBUTES` | api, worker, beat, conector | `deployment.environment=test` o `production`; la lee el SDK | no |
| `AGENT_CORE_ROLE` | cada rol | ya la fijan entrypoints y compose; ahora la lee también `Settings` | no |
| `SLACK_ALERTS_WEBHOOK_URL`, `SENTRY_*` | api, worker, beat, conector | sin cambio de semántica | no |

Sin `OTEL_EXPORTER_OTLP_HEADERS`: Alloy no pide credenciales en la red interna.

## Consecuencias

- **Apagado no cambia nada.** No hay SDK de métricas importado, ni consultas de sondas, ni middleware de instrumentación. El middleware de request id y el de duración HTTP corren siempre, pero son baratos y sin red.
- **El CI no prueba el SDK de métricas real.** El Python del runner (`/usr/local/Python-3.12.0`) no trae `_ctypes`. Esas pruebas usan `importorskip` y corren en un intérprete aparte en local; las de cableado sí corren en CI. Seguimiento: un Python completo en `preparar-entorno`. La imagen `python:3.12-slim` sí lo trae.
- **Instancias del worker.** Cada hijo del prefork y cada redeploy crea un `service.instance.id` nuevo. Las series viejas dejan de estar activas a los pocos minutos, pero un despliegue duplica por un rato las del worker.
- **Lo que mide el LLM.** `agentcore_llm_*` cubre las llamadas del loop del turno, primario y respaldo. Los reintentos internos de la política de modelo y los embeddings no se cuentan aquí; siguen en `usage_ledger`.

## Pendientes conocidos

- `model` se valida con una regex, no contra el catálogo de modelos. Un id de modelo afinado con nombre de organización pasaría como valor de etiqueta. Hoy no se configura ninguno.
- En rutas públicas (`/webhooks`, `/api/pack`) se aceptan el `X-Request-ID` y el `traceparent` del cliente. Un tercero podría mezclar correlaciones en Loki o Tempo, aunque no mezcla datos.
- `waweb_inbound_publicado` (`core/waweb/manager.py`, anterior a este ADR) registra el teléfono del cliente en el log. La redacción de Alloy lo tapa, pero conviene quitarlo en origen.

## Alternativas descartadas

- **Exponer `/metrics` (Prometheus pull).** Habría que publicar un puerto más por contenedor y que Alloy descubriera los contenedores de cada despliegue, cuyo nombre cambia. Además los hijos del prefork no comparten registro sin el modo multiproceso.
- **Que Grafana lea Postgres directo** para la DLQ y los crons. Sería una credencial de base fuera de la máquina, y tablas de negocio a una consulta de distancia. Las sondas exponen solo números agregados.
- **Etiquetar por negocio** (`tenant_id`). Es útil para soporte, pero son cientos de series por métrica y va contra el aislamiento. Por negocio se mira en trazas y logs, con `tenant_id` como atributo del span.
- **CeleryInstrumentor.** Duplicaría los spans que ya crea `celery_trazas.py`, y su propagación pisaría los headers del turno.

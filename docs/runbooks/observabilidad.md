# Observabilidad: encendido, verificación y qué hacer ante cada alerta (runbook)

ADR-0188. El backend manda métricas y trazas por OTLP a Grafana Alloy (`http://alloy:4318`, red Docker `coolify`), y Alloy reenvía a Grafana Cloud. Los logs los lee Alloy de Docker, los redacta y los manda a Loki y a Elasticsearch/Kibana.

- Alloy y su configuración, los dashboards, las reglas y la redacción viven en el repo `solvershub-infra`.
- Este runbook cubre la parte del backend y la respuesta a las alertas.
- **Producción la despliega y configura solo el dueño.** En test, una sesión solo despliega con OK y token del dueño.

## 1. Encender en test

Requisito: Alloy arriba en el host, con `container_name: alloy` en la red `coolify`. Para comprobarlo:

```bash
docker ps --filter name=alloy
```

Variables en Coolify para api, worker, beat y conector (si existe):

| Variable | Valor |
|---|---|
| `OTEL_ENABLED` | `true` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://alloy:4318/v1/traces` (URL completa) |
| `OTEL_METRICS_ENABLED` | `true` |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | `http://alloy:4318/v1/metrics` (URL completa) |
| `OTEL_SERVICE_NAME` | `agent-core-api`, `agent-core-worker`, `agent-core-beat` o `agent-core-connector` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=test` |
| `SLACK_ALERTS_WEBHOOK_URL` | el webhook de `#alertas-test` |

**No** se pone `OTEL_EXPORTER_OTLP_HEADERS`: las credenciales de Grafana Cloud están solo en Alloy.

Después hay que redesplegar los cuatro recursos. Un restart en Coolify reconstruye la imagen desde `develop`. Worker y beat deben quedar en el mismo commit que la API.

## 2. Verificar que llega

1. **Arranque.** En los logs de cada contenedor tiene que aparecer `metricas_configuradas` con `instancia` e `intervalo_s=60`. En el worker aparece una vez por hijo del prefork.
   - `metricas_sin_endpoint`: falta `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`.
   - `metricas_sdk_no_disponible`: el Python de la imagen no trae `_ctypes`, algo que no debería pasar con `python:3.12-slim`.
2. **Sondas.** En la API aparece `sondas_arrancadas`. Si sale `sonda_fallo` con `sonda=…` y `error_type`, esa medición no llega y solo ella falta.
3. **Salud profunda.** Desde el host, con todo sano debe devolver 200 y `{"status":"ok","componentes":{"db":"ok","redis":"ok","worker":"ok"}}`:

   ```bash
   curl -s http://127.0.0.1:3013/healthz/deep
   ```

   `https://test.solvershub.site/healthz/deep` no sirve para esta comprobación: devuelve el 404 del panel, porque el túnel de test no enruta esa ruta a la API.

4. **Grafana**, en Explore → Prometheus, con `deployment_environment="test"`. Si Grafana Cloud no promueve ese atributo del recurso a etiqueta, hay que filtrar uniendo con `target_info` (o promoverlo en Alloy):
   - `agentcore_dependency_up` en 1 para db y redis.
   - `agentcore_worker_heartbeat_age_seconds` por debajo de 120.
   - `agentcore_http_server_duration_seconds_count` creciendo.
5. **Turno de punta a punta.** Usar «Probar asistente» en el panel de test. En Tempo debe aparecer `kernel.turn → llm_call`, y `agentcore_llm_cost_usd_total` debe subir.
6. **Correlación.** Una línea de log de una petición lleva `request_id` y `trace_id`, y su `trace_id` abre la traza en Tempo.
7. **Redacción.** Ningún span lleva `access_token`, `client_secret`, `hub.verify_token` ni la query de una URL. En Tempo, buscar por esos textos debe dar cero resultados.
8. **Presupuesto.** En la pestaña de uso de Grafana Cloud, las series activas deben quedar por debajo de 3k en total. Si pasa de 8k salta la alerta de presupuesto: ver §4, Presupuesto del plan.

## 3. Apagar

- `OTEL_METRICS_ENABLED=false` apaga métricas y sondas.
- `OTEL_ENABLED=false` apaga las trazas y la autoinstrumentación.

Hay que redesplegar. No hay datos que limpiar en el backend.

## 4. Qué hacer ante cada alerta

Cada alerta llega a Slack con el entorno. En `#alertas-prod` el on-call es el dueño.

| Alerta | Señal | Primer paso | Siguiente |
|---|---|---|---|
| Worker sin latido (> 180 s) | `agentcore_worker_heartbeat_age_seconds` | ¿El contenedor del worker está arriba? (`docker ps`, Coolify) | Si está arriba y no late: logs del worker (Celery conectado a Redis, `core.worker_heartbeat` en el beat). Si el beat está caído, el latido tampoco se escribe |
| DLQ > 0 durante 5 min | `agentcore_dlq_length` | Panel → DLQ del negocio afectado: motivo de cada entrada | Re-encolar desde el panel una vez corregida la causa. Las entradas sembradas por los escenarios QA no cuentan |
| Dependencia caída | `agentcore_dependency_up == 0` | `/healthz/deep` dice cuál | Postgres o Redis del entorno en Coolify: estado, disco y memoria. La API sigue viva porque `/healthz` no depende de ellos |
| p99 del webhook > 1 s | `agentcore_http_server_duration_seconds{route_group="webhooks"}` | Tempo: spans lentos de `POST /webhooks/*` | El webhook solo encola. Si es lento, suele ser Redis o la base (spans de SQL) |
| 5xx > 5 % durante 10 min | `status_class="5xx"` | Loki: `{service="api", level="error"}` en la ventana | Sentry del proyecto. Con `request_id` se encuentran todas las líneas de la petición |
| Costo LLM de la hora > 3× el promedio de 7 días y > 1 USD | `agentcore_llm_cost_usd_total` | ¿Qué `model` subió? ¿Hay un bucle de turnos? Revisar `agentcore_llm_calls_total` y `turn_duration` | Pausar el agente o la conexión del negocio desde el panel si es un bucle. Revisar `usage_ledger` por negocio, que no está en las métricas |
| Cron crítico fallido | `agentcore_cron_failures_24h{job}` | Panel → Crons → historial del job: clase del error | Ejecutarlo a mano desde el panel cuando la causa esté corregida (ADR-0183) |
| Errores de Graph | `agentcore_graph_api_errors_total{category}` | `auth`: token vencido o revocado (salud de conexiones). `rate_limit`: esperar, el backoff ya lo respeta | `graph_api_calls` en la base tiene el detalle por llamada |
| Synthetic falla en ≥ 2 sondas | Synthetic Monitoring | ¿Túnel de Cloudflare caído? Ver el runbook del túnel y el watchdog | Si el túnel está bien: la API (contenedor y `/healthz`) |
| Contenedor caído o ausente | cAdvisor (Alloy) | Coolify: estado del recurso y último despliegue | Si cayó tras un despliegue, revertir al despliegue anterior en Coolify |
| Alloy caído (`absent(node_uname_info)`) | Grafana Cloud | `docker ps --filter name=alloy` y `docker logs alloy` en el host | Sin Alloy no llega nada; solo Synthetic alerta desde fuera |
| Disco > 80 % (aviso) / > 85 % (crítico) | node exporter | Liberar espacio: imágenes viejas y cachés (lista en el plan, paso 0) | Por encima de 85 %, Elasticsearch pone los índices en solo lectura: ver §5 |
| Presupuesto del plan (> 8k series o > 40 GB de logs al mes) | uso de Grafana Cloud | ¿Qué métrica creció? En Explore, `count by (__name__)({__name__=~"agentcore_.*"})` | Una etiqueta con valores nuevos es un bug: ninguna lleva ids (ADR-0188 §3). Si el exceso viene del worker, agregar fuera `service.instance.id` o bajar su concurrencia en test |

## 5. Elasticsearch en solo lectura

Con el disco por encima del umbral, Elasticsearch marca los índices con `index.blocks.read_only_allow_delete`.

1. Bajar el disco del 80 %.
2. Quitar el bloqueo y confirmar que `_cat/indices` vuelve a crecer. Los comandos están en `solvershub-infra` (`scripts/es_bootstrap.sh`).

Los logs no se pierden en Loki mientras tanto.

## 6. Rotación y accesos

- **Grafana Cloud.** El token de Alloy y el del script de dashboards se rotan en la consola de Grafana Cloud. Se actualiza el `.env` del host (`chmod 600`) y se reinicia Alloy. El backend no cambia.
- **Kibana.** Detrás de Cloudflare Access, y dentro con usuarios del rol `solvershub_logs_reader`. La contraseña de `elastic` la guarda solo el dueño.
- **Agregar una app nueva a los logs.** Su `applicationUuid` va a la lista fija de Alloy (`service`), en `solvershub-infra`.

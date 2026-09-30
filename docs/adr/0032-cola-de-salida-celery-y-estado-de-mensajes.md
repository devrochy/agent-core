# ADR-0032 — Cola de salida vía Celery, idempotencia por client_message_id y máquina de estados de mensajes

- **Estado:** aceptado
- **Fecha:** 2026-09-20

## Contexto

El ticket pedía un pipeline de mensajería con BullMQ: cola de entrada,
cola de salida con idempotencia y rate limiting alineado a Meta, y un
worker desplegable en un proceso separado (Railway/Fly/Render). Un
research previo (agente Explore) confirmó que el pipeline de ENTRADA ya
está completo y es sólido (`core/gateway/webhooks.py` +
`core/orchestrator/consumer.py`, con 3 capas de idempotencia y un DLQ real
en `agent_core:inbound:dlq`) -- no hacía falta reconstruirlo. El gap real
estaba en la SALIDA: `core/gateway/sender.py::send_to_conversation` enviaba
síncrono e inline, sin idempotencia propia, sin usar la clasificación de
errores de Meta ya existente en `core/gateway/meta_errors.py`, y sin
aplicar nunca el estado real de entrega (`sent`/`delivered`/`read`) que
llega por el webhook `statuses`.

## Decisión

1. **La cola de salida se traduce a una tarea Celery** (`core.outbound_send`,
   `core/tasks/outbound_send.py`), no a un Redis Stream nuevo. Celery ya
   corre en un servicio `celery-worker` separado del API en
   `docker-compose.yml` -- eso YA satisface "worker en un proceso separado
   desplegable independientemente" sin agregar un `Dockerfile` ni un
   segundo Redis Stream a mano.
2. **Idempotencia**: `Message.client_message_id` se genera y persiste
   (`status='queued'`) ANTES de encolar la tarea. Un reintento de la
   MISMA tarea Celery relee la fila por id y hace no-op si ya está en un
   estado terminal -- doble protección (constraint único + chequeo en
   `_send_one`).
3. **Rate limiting alineado a Meta**: se reusa tal cual
   `core/gateway/meta_errors.py::classify_and_build_error`/
   `_retry_after_from_usage` (ya leía `estimated_time_to_regain_access` del
   header `X-Business-Use-Case-Usage`) -- se agregó el código 130429
   (throughput excedido) a `RATE_LIMIT_CODES`, y un branch explícito para
   131047 (re-engagement, semántica OBSERVADA no verificada contra doc
   primaria 2026) que nunca reintenta. La decisión de reintentar/countdown
   vive en una función PURA (`decide_retry`), separada de la mecánica de
   Celery, para que sea testeable sin simular un `self` de Celery real.
4. **Máquina de estados de `statuses`**: nuevo módulo
   `core/gateway/meta_message_status.py`. Mismo problema chicken-and-egg ya
   resuelto para plantillas (ADR-0031): una quinta función `SECURITY
   DEFINER`, `resolve_tenant_for_message(external_message_id)`, resuelve
   el tenant antes de abrir `tenant_scope`. Orden monótono
   `queued<sent<delivered<read<failed`: un status nuevo se aplica solo si
   supera el rank actual O si su timestamp es posterior al de la última
   actualización -- nunca por mero orden de llegada.
5. **Ventana de servicio (24h)**: `Conversation.service_window_expires_at`
   se refresca en cada inbound de WhatsApp
   (`consumer.py::_persist_inbound`) y se valida ANTES de encolar un envío
   libre (`sender.py::validate_service_window`) -- evita gastar un ciclo
   de cola/rate-limit en un envío que Meta rechazaría con 131047 seguro.

## Hallazgo colateral corregido

Al tocar `sender.py::resolve_credentials` se encontró un bug preexistente
real: el fallback de "conexión primaria en BD" no filtraba por
`tenant_id` en absoluto -- con más de un `ChannelConnection` `is_primary=True`
en la base (aunque de tenants distintos), `scalar_one_or_none()` revienta
con `MultipleResultsFound`. Se agregó el filtro explícito
`ChannelConnection.tenant_id == get_current_tenant_id()`.

## Hallazgos del auditor-multitenant (2026-09-20) corregidos

- **Medio**: `resolve_credentials`'s rama de `connection_id` explícito
  (conexión manual) leía la fila con `session.get()` sin comparar
  `tenant_id` contra `expected_tenant_id` -- se agregó el mismo chequeo que
  ya tenía el fallback de "primaria" y `_resolve_meta_connection_credentials`.
- **Medio**: `core/tasks/outbound_send.py::_send_one`/`_mark_failed`
  confiaban ciegamente en el `tenant_id` (string plano, sin ligadura
  criptográfica al `message_id`) pasado como argumento de la tarea Celery,
  sin comparar contra `Message.tenant_id` tras el `session.get()` -- un
  desajuste hoy se resolvería como un no-op silencioso de RLS, no como un
  error visible. Se agregó el chequeo explícito con `log.error` si difieren.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Nuevo Redis Stream `agent_core:outbound` + consumer asyncio propio | Duplicaría la infraestructura de reintentos/countdown que Celery ya da gratis, y el ticket pide justamente reintentos con backoff configurable -- Celery lo hace nativo. |
| Un Dockerfile/servicio nuevo para el worker de salida | `celery-worker` ya es un servicio separado en `docker-compose.yml`; no hay necesidad real de aislar el envío en un TERCER proceso todavía. |
| DLQ como stream de Redis (como el de entrada) | El volumen de fallos de salida es bajo y ya hay una fila de `Message` auditable por diseño (`sender.py`, "todo fallo queda auditado en la fila") -- se extendió ese criterio con columnas de error reales en vez de reinventar un stream. |

## Consecuencias

**Positivas:** el envío ya no bloquea el proceso que atiende webhooks/HTTP;
reintentos y rate limiting respetan la señal real de Meta en vez de un
backoff ciego; el estado de entrega real queda visible en el panel.

**Negativas / lo que aceptamos pagar:** `send_to_conversation` cambia de
contrato ("entregado" -> "encolado") -- los call-sites que dependían del
`bool` para decidir escalar ya no lo hacen directamente, la escalación por
fallo real se movió dentro de la tarea.

**Qué haría que revisáramos esto:** si el volumen de envíos crece al punto
de necesitar un `celery-worker` DEDICADO solo a `core.outbound_send`
(cola separada de la de cron), separar colas de Celery por nombre en vez
de compartir la default.

## Impacto en el código

- `core/gateway/sender.py`, `core/tasks/outbound_send.py`,
  `core/gateway/meta_message_status.py`, `core/gateway/webhooks.py`,
  `core/gateway/channels/whatsapp/outbound.py`,
  `core/orchestrator/consumer.py`, `core/gateway/meta_errors.py`.
- `alembic/versions/0027_outbound_queue_y_status.py`.

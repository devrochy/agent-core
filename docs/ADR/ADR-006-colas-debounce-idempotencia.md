# ADR-006 · Redis Stream + debounce ZSet + idempotencia en 3 capas

**Estado**: Aceptada · **Fecha**: 2026-08-19

## Contexto

WhatsApp reintenta webhooks, los clientes escriben en ráfagas y el LLM tarda segundos.
agent-sale resolvió los tres problemas en producción (ADR-002/022 y las 3 capas de
idempotencia); agent-core adopta los mismos patrones.

## Decisión

1. **Cola**: Redis Stream `agent_core:inbound` + consumer group. Re-entrega máx 3,
   luego dead-letter stream (`:dlq`). El webhook solo verifica firma, deduplica y
   encola: responde 200 en milisegundos.
2. **Debounce**: Sorted Set `debounce:pending` (score = timestamp de disparo).
   `ZADD` sobre un member existente ACTUALIZA el score → cada mensaje nuevo reinicia
   el timer gratis. Claim atómico con `ZREM` (safe multi-worker). Payload en clave
   aparte. Crash recovery al arrancar: conversaciones con inbound pendiente sin timer
   se reprograman con delay 0. Modos: inmediato/0s, rapido/5s, natural/15s, pausado/30s.
3. **Idempotencia en 3 capas**:
   - Capa 1: `SET inbound:{provider}:{message_id} NX PX 48h` en el gateway (fail-closed).
   - Capa 2: re-entrega limitada del consumer group + DLQ.
   - Capa 3: UNIQUE en Postgres (`messages.idempotency_key`; los plugins usan
     `context.idempotency_key` en sus escrituras).

## Justificación

- Celery para el path del mensaje agregaría serialización y latencia sin control fino
  de re-entrega; Streams dan XREADGROUP/XACK explícitos.
- El patrón ZADD-actualiza-score elimina toda la lógica de "cancelar y reprogramar
  timers": es una propiedad de la estructura de datos.
- Fail-closed en la Capa 1: ante Redis caído preferimos NO encolar a duplicar una
  operación de negocio.

## Consecuencias

- (+) Sin dobles respuestas, sin respuestas desordenadas en ráfagas, sin pérdida en crash.
- (−) Un turno diferido que falla no se reintenta (el próximo mensaje del cliente
  genera turno fresco) — límite conocido y documentado, heredado de agent-sale.

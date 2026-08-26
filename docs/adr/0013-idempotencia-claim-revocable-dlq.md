# ADR-0013 — Claim de idempotencia revocable para DLQ (extiende ADR-0007)

- **Estado:** aceptado
- **Fecha:** 2026-08-26
- **Extiende:** ADR-0007

## Contexto

ADR-0007 exige insertar `processed_events` con UNIQUE antes de procesar. Un evento que falla
va a la dead-letter con reintento manual (ROADMAP Sprint 1). Si el claim de idempotencia es
permanente, el reintento no puede volver a procesarse: el UNIQUE lo traga.

## Decisión

El claim en `processed_events` es **revocable**: columna `deleted_at` + índice único parcial
`(tenant_id, external_message_id) WHERE deleted_at IS NULL`. El reintento manual del DLQ hace
soft-delete del claim antiguo y re-inserta uno fresco; el índice parcial permite re-insertar.
El dedupe del consumer usa `INSERT ... ON CONFLICT DO NOTHING`; un conflicto es un duplicado
real y se descarta silenciosamente.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Claim permanente | El DLQ no puede reintentar nada que haya pasado el dedupe |
| Borrado físico del claim | Invariante #8: sin borrado físico en tablas de negocio |
| Claim con TTL | Redis como única fuente; se pierde la garantía dura de Postgres |

## Consecuencias

**Positivas:** el ciclo de vida del claim (crear → fallar → revocar → reintentar) vive en un
solo lugar; se respeta la invariante #8.

**Negativas / lo que aceptamos pagar:** dos filas por evento reintentado (histórico + claim
fresco); el `deleted_at` del claim requiere limpieza periódica.

**Qué haría que revisáramos esto:** que el soft-delete de claims crezca desproporcionado al
volumen de fallos reales — señal de que el procesamiento falla más de lo razonable.

## Impacto en el código

Migración del esquema core (`processed_events`), `core/orchestrator/consumer.py`,
endpoint del panel para reintento de DLQ.
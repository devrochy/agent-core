# ADR-0008 — `usage_ledger` append-only

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

La facturación y el margen dependen de saber exactamente qué costó cada conversación. Si el
registro de uso se edita, se actualiza o se pierde, la atribución de costos por tenant deja
de ser defendible.

## Decisión

Tabla `usage_ledger` **append-only**: una fila por turno con modelo, tokens (incluidos los
cacheados), tools llamadas, costo, latencia y resultado. Inmutable por trigger; es la fuente
de la facturación y del análisis de margen.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla de uso actualizable | El costo "histórico" cambia retroactivamente; no se puede auditar |
| Llevar el uso en logs | Los logs rotan y se borran; no son una fuente consultable |
| Agregados diarios | Se pierde la trazabilidad por conversación y por tool |

## Consecuencias

**Positivas:** atribución exacta por tenant y conversación; el panel muestra costo en vivo;
base para reportes de margen y para el cliente.

**Negativas / lo que aceptamos pagar:** la tabla crece sin fin (se debe archivar o particionar
por mes); toda corrección de costo es un asiento nuevo, no un update.

**Qué haría que revisáramos esto:** que una decisión de facturación requiera leer más de una
fila por turno — señal de que el asiento no capturó todo el costo.

## Impacto en el código

Tabla `usage_ledger` (append-only, `tenant_id`, RLS), `core/llm/*` (registro de tokens y
costo), panel `apps/agentos` (costos), y el contrato `AgentTurnResult.usage`.
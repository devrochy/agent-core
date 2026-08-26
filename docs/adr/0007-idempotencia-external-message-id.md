# ADR-0007 — Idempotencia por `external_message_id`

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Meta reintenta webhooks ante fallos de red o de nuestro lado. Si el mismo mensaje entra dos
veces, se duplican pedidos, se duplican costos en tokens y se rompen las reglas de negocio.

## Decisión

Tabla `processed_events` con restricción única sobre `external_message_id`, insertada
**antes** de procesar. El webhook hace el dedupe temprano en Redis (SET NX) y el UNIQUE de
Postgres es la última línea de defensa.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dedupe solo en Redis | El estado se pierde con un flush; no es una barrera dura |
| UNIQUE solo en Postgres | El costo de tokens ya se pagó cuando se detecta el duplicado |
| Confiar en la ventana de reintentos de Meta | No cubre redeliveries retrasados ni reprocesos manuales |

## Consecuencias

**Positivas:** un evento se procesa exactamente una vez; el insert-then-process hace que el
fallo a mitad de proceso no reprocese; base para auditoría de eventos.

**Negativas / lo que aceptamos pagar:** una fila por evento (crece con el volumen; se puede
archivar); el insert añade una escritura antes del trabajo real.

**Qué haría que revisáramos esto:** que un turno de conversación legítimo se descarte por
idempotencia — señal de que la clave es demasiado amplia o se reutiliza.

## Impacto en el código

`core/gateway/idempotency.py`, `core/gateway/webhooks.py`, tabla `processed_events`
(`external_message_id` unique, `tenant_id`), y el contrato `InboundMessage.externalMessageId`.
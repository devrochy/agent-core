# ADR-0006 — Lock + debounce por conversación en Redis

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

WhatsApp entrega en ráfaga: varios mensajes del cliente llegan en segundos. Sin control, el
agente responde tres veces y se contradice; y un solo turno por mensaje multiplica el costo
en tokens.

## Decisión

Lock distribuido por `conversationKey` más ventana de debounce de 2–4 s que agrupa mensajes
consecutivos en un solo turno. El timer vive en Redis (ZSET con score = próximo disparo);
cada mensaje nuevo reinicia el timer.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Responder a cada mensaje | Respuestas duplicadas y contradictorias; costo ×N |
| Lock solo (sin debounce) | No agrupa la ráfaga; el agente sigue contestando mensaje a mensaje |
| Debounce en memoria del proceso | Se pierde al reiniciar el worker o con múltiples workers |

## Consecuencias

**Positivas:** un turno por ráfaga; fairness por tenant (una cola no ahoga a otra); el
estado del timer sobrevive a reinicios porque vive en Redis.

**Negativas / lo que aceptamos pagar:** latencia adicional de 2–4 s por mensaje (aceptable en
WhatsApp); un mensaje que llega justo al cerrar la ventana puede esperar un poco más;
recuperación de timers huérfanos al arrancar.

**Qué haría que revisáramos esto:** clientes que perciban la demora del debounce como lentitud
— señal para bajar la ventana o hacerla configurable por tenant.

## Impacto en el código

`core/orchestrator/debounce.py`, `core/orchestrator/consumer.py`, Redis (ZSET), y la
configuración `DEBOUNCE_DELAY_MS` (por tenant, no global).
# ADR-0090 — Reclamo automático de pendientes y claim de idempotencia con estado

- **Estado:** aceptado
- **Fecha:** 2026-10-04
- **Extiende:** ADR-0007 (claim de idempotencia), ADR-0013 (claim revocable para DLQ)
- **Contexto:** HU-ARCH-06 (SPEC-01)

## Contexto

El consumer del stream de entrada (`core/orchestrator/consumer.py`) usaba
`xreadgroup` con `>` (solo mensajes NUEVOS) y `CONSUMER = "orchestrator-1"` fijo.
Consecuencias observadas en el incidente del 2026-10-04 (pruebas E2E del
calendario): varios mensajes quedaron `pending` para siempre; la DLQ nunca se
alcanzaba (el contador de reintentos solo sube si la entrada se re-entrega, y
nada la re-entregaba); y con dos réplicas el nombre compartido impedía
distinguir consumidores vivos. Además, `processed_events` no distinguía
"terminado" de "en vuelo": si el proceso moría a mitad de un turno, el claim
quedaba insertado y un reintento lo veía como duplicado (pérdida silenciosa).

## Decisión

1. **Reclamo automático (`XAUTOCLAIM`)** cada 30 s en `consume_loop`: los
   pendientes con `idle >= INBOUND_RECLAIM_IDLE_MS` (default 5 min) se reclaman
   y procesan como cualquier entrada, con `_claim_event` como guarda.
2. **Nombre de consumer único por réplica** (`orchestrator-<host>-<pid>`).
3. **Claim con estado:** `processed_events.completed_at` (migración 0049).
   - `NULL` = en vuelo o abandonado; con fecha = terminado.
   - `_claim_event` devuelve `claimed` (nuevo o abandonado revocado),
     `duplicate` (terminado) o `busy` (incompleto reciente: otro worker en
     vuelo → **sin ACK**, el reclamo lo reintenta).
   - Al fallar un turno, el claim se **revoca** de inmediato (ADR-0013) para que
     el siguiente reclamo reprocese sin esperar al umbral de abandono; el
     contador de reintentos (`inbound:retries:*`) gobierna los intentos.
4. **DLQ con contexto:** al agotar `MAX_DELIVERIES` (3) el mensaje va a
   `agent_core:inbound:dlq` con `error`, `deliveries` y `failed_at`, se hace
   `XACK` y se revoca el claim (el reintento manual del panel sigue vigente).
5. **Invariante de configuración:** `CLAIM_STALE_SECONDS` (default 600 s) DEBE
   ser mayor que `INBOUND_RECLAIM_IDLE_MS` (300 s): así un turno legítimamente
   largo no se reprocesa en paralelo. HU-ARCH-27 acotará la duración máxima del
   turno (timeout) y permitirá endurecer estos valores.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Reintentar con `xreadgroup` sobre `0` (leer el PEL propio) | Solo ve el PEL de ESTE consumer; un pendiente de una réplica muerta no se recupera |
| Re-encolar a mano al detectar timeout | Duplica el mensaje en el stream y complica la idempotencia; `XAUTOCLAIM` es la primitiva nativa para esto |
| Claim con TTL en Redis en vez de `completed_at` en Postgres | Rompe ADR-0013 (Postgres es la garantía dura) y perdería el estado en un flush |
| ACK optimista al reclamar | Un turno fallido se perdería sin pasar por la DLQ |

## Consecuencias

**Positivas:** ningún mensaje queda `pending` para siempre; la DLQ es alcanzable
y con contexto de error; el reclamo es seguro entre réplicas (busy/duplicate);
la observabilidad (`inbound_reclaimed`, `inbound_busy_skip`,
`message_dead_lettered`) hace visible lo que antes era silencioso.

**Negativas / lo que aceptamos pagar:** más filas en `processed_events` (una por
intento, ya previsto por ADR-0013); el reclamo escanea el PEL con
`start_id="0-0"` y `count=10` por ciclo (suficiente para el volumen actual;
si el PEL crece, habrá que paginar con el cursor que devuelve `XAUTOCLAIM`).

**Qué haría que revisáramos esto:** que el PEL tenga cientos de pendientes
crónicos (señal de turnos que fallan sistemáticamente) o que la latencia del
reclamo importe (mover a un reclaim dirigido por `idle` por entrada).

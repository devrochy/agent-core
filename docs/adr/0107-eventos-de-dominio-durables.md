# ADR-0107 — Eventos de dominio durables con outbox

- **Estado:** aceptado
- **Fecha:** 2026-10-07
- **Enmienda:** ADR-0067 (eventos de pago del core): `payment.confirmed`/`payment.rejected` dejan de
  viajar solo por el bus in-process; pasan por el outbox de `domain_events`.
- **Contexto:** HU-SUB-03: ningún pago confirmado puede perder su efecto (activar un plan, registrar un abono).

## Contexto

El webhook de Wompi responde 200 después de cambiar el estado de la transacción y de emitir
`payment.confirmed` por el bus **in-process** (`pack_payments/module.py`). Si un handler falla,
`EventBus.emit` lo traga (`log.exception` y continúa, `core/shared/events.py:58-65`) y Wompi no
reintenta porque ya recibió 200: el cliente pagó y el efecto (plan activo, abono registrado) nunca
ocurre. Es dinero cobrado sin servicio entregado.

Además `pack_cobranza` se suscribía directo al bus (`pagos.py::suscribir()` desde `module.py`) y no
se des-suscribía al desactivar el pack.

## Decisión

1. **Dos caminos de eventos, por clase:**
   - **Eventos del core** (`message.sent`, `escalation.triggered`, `tool.executed`, `plugin.*`…):
     siguen en el bus in-process — son observabilidad/operación de un turno, no efectos de dinero.
   - **Eventos de dominio** (`payment.*` hoy; `order.*` en HU-COM-05, `subscription.*` después):
     van por el **outbox** `domain_events` y se entregan con reintentos. Un pack los emite con
     `emit_domain` **en la misma transacción** que su cambio de estado.
2. **Tabla `domain_events`** (migración `0056`): `tenant_id` NOT NULL + RLS forzada + policy
   (invariante 1), `topic`, `payload` JSONB (con `tenant_id` explícito dentro), `status`
   (`pendiente|entregado|fallido`), `attempts`, `next_attempt_at`, `dedupe_key` (único por tenant) y
   `last_error`. Índice `(tenant_id, status, next_attempt_at)` para el despachador.
3. **Escritura idempotente**: `INSERT … ON CONFLICT (tenant_id, dedupe_key) DO NOTHING`. Para pagos,
   `dedupe_key = payment:{provider_reference}:{status}`: un reenvío de Wompi por red (mismo evento)
   no duplica, y el estado solo cambia en transiciones reales (como hoy).
4. **Despachador Celery** (`core.tasks.domain_events_dispatch`, beat 30 s): enumera pendientes vencidos
   con la función `SECURITY DEFINER admin_domain_events_due()` (solo `id`+`tenant_id`, patrón ADR-0012),
   reclama cada evento en Redis y lo entrega bajo `tenant_scope` re-emitiendo por el bus in-process —
   los handlers existentes **no cambian de firma**. Éxito → `entregado`; fallo → `attempts+1` con
   backoff exponencial (30 s, 1 m, 2 m, 5 m, 10 m); al agotar **5 intentos** → `fallido` con
   `last_error` y alerta. `fallido` es visible en el panel ("Bandeja de fallos") y reintentable a mano.
5. **Idempotencia compartida**: la inserción evita duplicados; los handlers siguen siendo idempotentes
   por su propia cuenta (cobranza `idempotency_key=link-{reference}`, commerce valida referencia y
   `payment_status`, subscriptions por cargo). La entrega es **at-least-once**.
6. **Suscripciones con tracking**: los packs se suscriben con `host.subscribe(...)` (ya el caso de
   commerce/subscriptions); `pack_cobranza` migra de `event_bus.subscribe` directo, y
   `MCPHost.remove_plugin` limpia también los handlers de eventos del plugin desactivado.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Migrar TODOS los eventos al outbox | Los eventos del core son por-turno y de bajo valor: duplican escrituras y latencia sin retorno |
| Cola externa (Redis Streams/RabbitMQ) | Agrega infraestructura nueva; la tabla con RLS + Celery resuelve durabilidad y orden con lo que ya opera |
| Mantener emit in-process y solo reintentar en el webhook | No es transaccional: el 200 ya se respondió y el proceso puede morir entre el cambio y el emit |
| Reintentos infinitos | Un evento tóxico bloquearía el despachador; 5 intentos + DLQ visible es el tope operativo (incident-thinker) |

## Consecuencias

**Positivas:** ningún pago confirmado pierde su efecto; el error de un handler no desaparece (queda en
`fallido` con su `last_error`); los efectos corren con el `tenant_scope` del evento, no del emisor.

**Negativas / lo que aceptamos pagar:**
- Latencia del efecto: hasta el próximo beat (≤30 s) tras el pago. Aceptable: los avisos al cliente
  no dependen de estos handlers y el link de pago ya está confirmado.
- At-least-once exige handlers idempotentes para siempre; el contrato queda documentado acá y en
  `core/events/outbox.py`.
- El despachador depende de Celery beat; si beat cae, los eventos quedan `pendiente` (visibles por
  edad en logs/métricas).

**Qué haría que revisáramos esto:** HU-COM-05 (eventos `order.*`), HU-SUB-14 (acciones
post-activación) o un volumen que justifique un broker externo.

## Impacto en el código

- `core/events/outbox.py` (nuevo): `emit_domain` + `DomainEventOutbox` helpers.
- `packages/pack-payments/pack_payments/module.py`: el webhook escribe el outbox en la misma sesión.
- `core/tasks/domain_events_dispatch.py` (nuevo) + beat en `celery_app.py`.
- `core/mcp/host.py::remove_plugin`: des-suscripción de handlers de eventos.
- `packages/pack-cobranza/pack_cobranza/module.py`/`pagos.py`: `host.subscribe` en vez de `suscribir()`.
- `core/api/routers/domain_events.py` (nuevo): listado de `fallido` y reintento manual.
- Migración `0056_domain_events.py` + `core/db/admin_enum.py::admin_domain_events_due`.
- Tests: `tests/test_domain_events_outbox.py`, `tests/test_domain_events_dispatch.py`, integración del
  webhook de pagos actualizada.

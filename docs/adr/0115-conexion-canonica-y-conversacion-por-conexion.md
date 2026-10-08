# ADR-0115 — Conexión canónica y conversación por (conexión, cliente)

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-11 (SPEC-26). Enmienda ADR-0069 (catálogo de tipos de conexión), ADR-0045 (medición de
  conversaciones) y ADR-0072 (gating por conexión). Respeta ADR-0095.

## Contexto

- Conviven dos modelos de conexión, `channel_connections` (genérico) y `meta_connections` (cuenta de Meta), con un puente doble en `conversations`.
- El webhook resuelve el **negocio** y no la conexión.
- La conversación abierta es una por cliente y se re-asigna a la última conexión por la que escribió (`core/orchestrator/consumer.py:562-602`).
- El chat web es configuración en `AppSettings`, no una conexión.

## Decisión

La conexión es `channel_connections`, y hay una conversación abierta por (conexión, cliente).

1. **`channel_connections` es la Conexión:** un extremo conversacional (número, página, widget o buzón). `meta_connections` queda como cuenta del proveedor, enlazada por `meta_connection_id` con FK compuesta `(tenant_id, meta_connection_id)`.
2. **Integraciones** (calendario, correo saliente): siguen en el catálogo con `scope` de negocio o de conexión (`bound_connection_id`) y no tienen agentes. El gating por conexión requerida (ADR-0072) mira las del negocio y las ligadas a la conexión de la conversación.
3. **Chat web** como tipo de conexión `webchat`. El endpoint público resuelve la conexión por su clave.
4. **Ruteo:**
   - El webhook resuelve la conexión, y hay **una conversación abierta por (conexión, cliente)**.
   - La memoria del cliente sigue siendo del negocio.
   - La métrica `conversaciones` (ADR-0045) cuenta una por conexión: un cliente que escribe a dos números inicia dos.
5. **Migración:**
   - *expand* y *backfill* por lotes e idempotente: cada número de Embedded Signup recibe su conexión y el chat web la suya;
   - *switch* detrás de `AOS_CONNECTION_ROUTING`: el consumer resuelve por (conexión, cliente) y deja de reescribir `connection_id`. Apagar la bandera es seguro hasta el *contract*: todavía no hay índice único, y el camino anterior toma la conversación abierta más reciente (`core/orchestrator/consumer.py:562-602`);
   - la vuelta atrás va en orden inverso: primero `AOS_RUNTIME` (ADR-0117) y después esta bandera. Al reescribir `connection_id`, el camino anterior limpia `release_id`, porque esa release es de la conexión anterior y la FK compuesta lo rechazaría;
   - *contract* tras 2 releases estables: se deduplican las conversaciones abiertas por (conexión, cliente), se crea el índice único parcial y `connection_id` pasa a obligatorio. Desde ahí no se vuelve al camino anterior.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla nueva `connections` | Toca todo el ruteo y el *sender* |
| Mantener el puente doble | No hay "la" conexión de un mensaje ni un Coordinador por conexión |
| Conversación por cliente | Rompe "un Coordinador por conexión" y ya causó respuestas por el canal equivocado |
| Crear el índice único en el *switch* | Con la bandera apagada, el camino anterior reescribe `connection_id`, choca con el índice y el mensaje termina en la DLQ |

## Consecuencias

**Positivas:** cada conexión tiene su Coordinador, sus agentes y su release; el cliente recibe respuesta por donde escribió; el chat web puede tener varias instancias.

**Negativas / lo que aceptamos pagar:** un cliente que escribe a dos números tiene dos conversaciones y consume dos de la cuota (la memoria del cliente las une); la migración de datos va detrás de una bandera; después del *contract* no hay vuelta atrás por bandera.

**Qué haría que revisáramos esto:** que el ruteo por conexión falle con activos de Meta compartidos (ADR-0095), que el backfill deje conversaciones sin conexión, o que contar por conexión encarezca la cuota de forma injusta para negocios con varios números.

## Impacto en el código

`core/gateway/webhooks.py` y `webhook_route_tenant.py` (resolución de conexión); `core/orchestrator/consumer.py` (`_resolve_conversation`); `core/orchestrator/loop.py` (gating con integraciones ligadas); `core/api/routers/web_chat.py`; `core/gateway/connection_types.py` (`webchat`); migraciones y backfill.

# ADR-0030 — `connection_wizards`: máquina de estados reanudable del Embedded Signup

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

El alta de WhatsApp por Embedded Signup es un flujo asíncrono basado en un
popup de Meta + `postMessage`: el admin puede cerrar la pestaña en cualquier
punto, y el `code` que Meta entrega para canjear por un token tiene un TTL de
30 segundos. Necesitamos saber en qué paso quedó cada conexión para poder
reanudar sin repetir el OAuth, y para poder mostrar "continuar donde lo
dejaste" si el popup se cerró sin terminar.

## Decisión

`connection_wizards` es una tabla de **negocio normal**: `tenant_id not null`
+ RLS + índice compuesto (a diferencia de `webhook_events`/
`meta_app_subscriptions`, que son tablas de sistema — este recurso SÍ tiene
un dueño claro, un tenant). Un único wizard "vivo" (`current_step != 'done'`)
por `(tenant_id, channel, environment)` a la vez (índice único parcial):
reanudar reusa la fila existente en vez de crear una segunda en paralelo.

**Regla dura**: `context` y `last_error` (JSONB) nunca contienen el `code` de
un solo uso ni ningún token de acceso. Son columnas legibles por RLS del
propio tenant — pensadas para reanudación y analítica de abandono (en qué
paso se cancela la gente), no para custodiar secretos. Los secretos viven
exclusivamente en `meta_private.meta_tokens` vía
`core/gateway/meta_token_store.py` (AAD tenant+conexión+tipo). Las funciones
que escriben en `connection_wizards` ni siquiera aceptan un parámetro `code`
o `token` — la regla está en la firma, no solo en la disciplina del caller.

**`coexistence`**: cuando el popup termina con el evento
`FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING` (WhatsApp Business App ya existente
que se integra sin migrar el número), se marca `coexistence: true` dentro de
`MetaConnection.health` (JSONB ya existente, mismo idioma que usa esa
columna hoy) y el paso `register_number` se marca completado sin ejecutarse
— llamar a `/register` en modo coexistence movería el número a Cloud API,
que es exactamente lo que ese modo evita.

**Puente `conversations.meta_connection_id`**: el envío saliente
(`core/gateway/sender.py::resolve_credentials`) resolvía credenciales
únicamente vía `Conversation.connection_id` (FK real a
`channel_connections.id`, el modelo de conexión manual). Una conexión hecha
por este wizard no tiene fila en `channel_connections`. En vez de migrar ese
FK (tabla caliente, con tráfico real, cambiarlo es más riesgoso), se agrega
una columna **paralela** `meta_connection_id` (nullable, FK a
`meta_connections.id`), poblada por `core/orchestrator/consumer.py` al lado
del lookup viejo. `resolve_credentials` prueba primero el puente nuevo, cae
al camino viejo si no hay nada — cero regresión para conexiones manuales.

**Actualización 2026-09-22 (auditor-multitenant, severidad alta)**:
`phone_numbers.phone_number_id` es único GLOBAL (no compuesto con
`tenant_id`) -- hoy es la única garantía real de que el puente
`meta_connection_id` no cruce tenants. Se agregó defensa en profundidad en
los dos puntos que dependían solo de esa unicidad: `core/orchestrator/
consumer.py` filtra `PhoneNumber` también por `tenant_id`, y
`core/gateway/sender.py::_resolve_meta_connection_credentials` recibe el
`tenant_id` de la CONVERSACIÓN que se está despachando y lo compara contra
`MetaConnection.tenant_id` antes de pedir el token -- si no coincide,
devuelve `None` (cae al fallback) en vez de descifrar y enviar con el token
de otro tenant. Ninguno de los dos cambios altera el comportamiento cuando
todo está bien formado; solo cierran la ventana si esa unicidad global
alguna vez se relaja.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Guardar el progreso del wizard dentro de `MetaConnection.health` (JSONB) en vez de una tabla nueva | `MetaConnection` no existe todavía en los primeros pasos del wizard (`select_channel`/`launch_oauth`, antes del canje) — no hay fila donde guardar nada hasta `exchange_code`. Necesitamos persistencia desde el primer clic. |
| Migrar `conversations.connection_id` para que apunte a `meta_connections` en vez de `channel_connections` | Cambiar el FK de una tabla con tráfico real y datos existentes es un riesgo mucho mayor que agregar una columna nueva nullable; además rompería `sender.py`/`consumer.py` para toda conexión manual existente el mismo día del deploy. |
| Un `CREATE TYPE wizard_step AS ENUM (...)` nativo de Postgres en vez de `CheckConstraint` | El resto del esquema usa `CheckConstraint` con `Text` para todos los "enums" (`status`, `channel`, `environment` de `meta_connections`, etc.) — un tipo nativo sería la única excepción, y agregar un valor nuevo al enum requiere `ALTER TYPE` (no transaccional en versiones viejas de Postgres) en vez de un `ALTER TABLE ... DROP/ADD CONSTRAINT` simple. |

## Consecuencias

**Positivas:** el wizard es reanudable de verdad (sobrevive a que se cierre
el navegador en cualquier punto); el envío saliente queda conectado al
resultado del wizard sin tocar `outbound.py` ni el modelo de credenciales
por-canal existente.

**Negativas / lo que aceptamos pagar:** dos rutas paralelas para resolver
credenciales de envío (`ChannelConnection` y `MetaConnection`+
`meta_token_store`) conviven en `sender.py` indefinidamente, hasta que una
tanda futura decida migrar las conexiones manuales viejas al modelo nuevo (o
no — ambas son válidas). **Qué haría que revisáramos esto de nuevo:** si se
agrega un tercer origen de credenciales de envío (ej. Messenger/Instagram
con su propio modelo), el `if/elif` de `resolve_credentials` debería
convertirse en una resolución por prioridad más explícita en vez de crecer
a mano.

## Impacto en el código

`alembic/versions/0023_connection_wizards.py`,
`alembic/versions/0024_conversations_meta_connection_bridge.py`,
`core/db/models.py` (`ConnectionWizard`, `Conversation.meta_connection_id`),
`core/gateway/meta_onboarding.py` (nuevo, única escritura de
`ConnectionWizard`), `core/orchestrator/consumer.py` (puebla el puente),
`core/gateway/sender.py::resolve_credentials` (lo consume),
`core/admin/views.py` (rutas `/admin/conexiones/wizard/whatsapp*`),
`tests/test_meta_onboarding_no_persiste_secretos.py`.

# ADR-0069 — Catálogo de tipos de conexión y generalización de `channel_connections`

- **Estado:** aceptado
- **Fecha:** 2026-09-25

## Contexto

`channel_connections` modela una conexión con dos columnas de texto libre
(`channel`, `provider`), sin categoría ni forma de declarar qué campos tiene cada
tipo. Sus credenciales se guardan contra una lista blanca fija
(`CREDENTIAL_KEYS = ("phone_number_id", "app_secret", "access_token", "verify_token")`,
`core/gateway/connections.py:22`), `is_primary` es global y el modal del panel lista
los canales cableados en la plantilla (`conexiones.html:151-204`, 1 de 5 activo).
Se quiere sumar correo y unificar Google Calendar como conexión, sin tocar el camino
de WhatsApp ni crear un tipo nuevo por cada proveedor.

## Decisión

Se introduce `connection_types`, una **tabla de sistema** (sin `tenant_id` ni RLS,
excepción deliberada con el mismo criterio que `plans`/ADR-0045), cuya fuente de
verdad es un catálogo tipado en `core/gateway/connection_types.py` y que
`sync_connection_types()` espeja vía `upsert_connection_type()` SECURITY DEFINER. La
tabla describe **presentación y credenciales** de cada tipo (`category`, `label`,
`icon`, `provider`, `setup_kind`, `produces_conversations`, `available`,
`credentials_schema`), no adaptadores de canal (eso sigue en `agent_registry` /
`agent_core.channels`, ADR-0052).

`channel_connections` gana `type_id` (FK lógica al catálogo) y `category`
desnormalizada **sin CHECK** (solo para el índice `(tenant_id, category, type_id)`;
la valida el join). `save_connection` valida las credenciales contra el
`credentials_schema` (JSON Schema, renderizable con el FormEngine) en vez de la lista
blanca fija, y el lookup por `external_id` filtra por `(tenant_id, type_id)`.

La **primacía** (`is_primary`) solo aplica a conexiones de la categoría de
mensajería (`comunicacion`): `primary_connection()`, `set_primary()` y el
auto-primary de `save_connection` la acotan a esa categoría, para que una conexión
de correo o calendario no desplace a la de WhatsApp. La resolución de credenciales
de un tipo se hace con un único helper
`resolve_connection_credentials(type_id, tenant_id)`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Solo `category` + agrupar en la plantilla | Agregar un tipo sigue siendo tocar plantilla y código; no declara credenciales ni disponibilidad |
| Plegar `meta_connections` al modelo genérico | Refactor del camino caliente de WhatsApp/Meta (ADR-0030 permite dos rutas paralelas); alcance y riesgo desproporcionados |
| Una tabla/consulta por proveedor | No clasifica ni unifica; multiplica el costo por tipo nuevo |
| `category` con CHECK en `channel_connections` | Duplica la categoría que ya vive en el catálogo y diverge al agregar una categoría |

## Consecuencias

**Positivas:** agregar un tipo es una fila en el catálogo (o editarlo en el código y
sincronizar), sin tocar la tabla de conexiones ni el contrato; el modal y el
formulario se generan desde datos; "Próximamente" deja de ser HTML; se puede
clasificar y auditar por categoría.

**Negativas / lo que aceptamos pagar:** una tabla de sistema más (que debe acotarse a
describir conexiones, no a convertirse en registro global de canales); el catálogo
tiene doble fuente (código + tabla espejo) que hay que sincronizar.

**Qué haría que revisáramos esto:** que el catálogo empiece a necesitar lógica de
routing o de adaptador (→ volver a ADR-0052) o que un tipo requiera credenciales
multipartita como Meta (→ repensar `credentials_schema`).

## Impacto en el código

- `core/gateway/connection_types.py` (nuevo), `core/gateway/connections.py`,
  `core/main.py` (sync en el lifespan), `core/db/models.py` (`ChannelConnection`).
- Migración core `0038` (+ `.down.sql`).
- Sin cambios en `packages/contracts` (el valor `"email"` de `ChannelKind` ya existe).

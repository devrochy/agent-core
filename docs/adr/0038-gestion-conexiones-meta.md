# ADR-0038 — Activar/desactivar/eliminar conexiones de Meta (soft-delete)

- **Estado:** aceptado
- **Fecha:** 2026-09-21

## Contexto

Con el modelo legado `ChannelConnection`, el panel siempre permitió
activar/desactivar y eliminar una conexión desde `/admin/conexiones`.
Al unificar la lista con `MetaConnection` (ADR previo de la lista
unificada), esas conexiones (Embedded Signup y BYOK) quedaron sin
ninguna acción de gestión -- solo un link a "Ver salud". El usuario lo
notó de inmediato al terminar de probar el wizard BYOK: "quiero poder
gestionar las conexiones activas, antes lo podíamos hacer".

## Decisión

1. **Activar/desactivar reusa el `status` que ya existía**
   (`CheckConstraint` de `MetaConnection` ya incluye `'connected'` y
   `'revoked'`) -- no hace falta ningún campo nuevo para esto, solo las
   rutas `POST /conexiones/meta/{id}/activar` y `.../desactivar`.
2. **Eliminar es soft-delete** (invariante nº8 de CLAUDE.md): columna
   `deleted_at` nueva (migración `0032`), nunca se borra la fila.
3. **El `UniqueConstraint(tenant_id, channel, environment, meta_app_id)`
   pasa a ser un índice único PARCIAL** (`where deleted_at is null`) --
   sin este cambio, "eliminar" una conexión y volver a conectar la
   MISMA app (mismo `meta_app_id`) chocaría contra el unique de la fila
   soft-deleteada. El índice sigue incluyendo `tenant_id` como primera
   columna -- no se toca el aislamiento por tenant, solo se agrega la
   condición de "viva".
4. **Las tres rutas nuevas siguen el patrón ya establecido**: `tid =
   get_current_tenant_id()` + `get_owned(session, MetaConnection, id,
   tid)` -- mismo criterio que `diagnosticar`/`resubscribe-waba`/
   `billing`, ya existentes en `/admin/conexiones/salud`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Reusar `status="error"` en vez de un soft-delete real para "eliminar" | Confunde dos conceptos distintos: "esta conexión tuvo un fallo" (diagnóstico) vs. "el admin decidió sacarla de la lista a propósito" (intención explícita) -- necesitaban ser distinguibles para saber si vale la pena reintentar sola. |
| Borrado físico de la fila | Viola la invariante nº8 de CLAUDE.md sin excepción aplicable (no es `tenant purge`). |

## Consecuencias

**Positivas:** paridad de gestión entre `ChannelConnection` y
`MetaConnection` en la lista unificada; se puede reconectar la misma
app de Meta después de "eliminar" una conexión vieja sin chocar con el
unique.

**Negativas / lo que aceptamos pagar:** `WabaAccount`/`PhoneNumber`/
`meta_token_store` asociados a una `MetaConnection` eliminada NO se
limpian automáticamente (quedan huérfanos, referenciando una conexión
`deleted_at != null`) -- aceptable porque nunca se leen fuera del
contexto de esa conexión, y limpiarlos en cascada es scope de un
ticket aparte si hiciera falta liberar espacio o PII.

**Qué haría que revisáramos esto:** si se necesita ELIMINAR de verdad
(purga real, no soft-delete) por un pedido de borrado de datos de un
cliente, corresponde el mecanismo de `tenant purge` documentado en
`docs/MULTITENANT.md`, no esta ruta.

## Impacto en el código

- `alembic/versions/0032_meta_connections_soft_delete.py` (nuevo).
- `core/db/models.py` (`MetaConnection.deleted_at`, índice único
  parcial).
- `core/admin/views.py` (3 rutas nuevas, filtro `deleted_at.is_(None)`
  en `connections_page`/`meta_health_page`).
- `core/admin/templates/conexiones.html` (botones de gestión para
  filas `meta_connection`).

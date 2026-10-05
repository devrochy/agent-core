# ADR-0097 — Sync incremental y auditoría móvil de la app oficial

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Contexto:** HU-APP-15 (SPEC-25, solvershub-docs)

## Contexto

La app oficial (SPEC-25) debe funcionar con conexión intermitente en todos los módulos: leer datos
cacheados sin red, encolar escrituras y sincronizar sin duplicados ni pérdidas. El campo ya tiene
su motor offline (cola idempotente + ack en `/sync-offline/eventos`); los módulos del panel no
tienen sync incremental (hoy no existe ningún endpoint con cursor en `/api/v1`). Además, las
acciones críticas hechas desde la app (borrar cuota, liquidar, cambiar plan) deben quedar
auditadas aunque se ejecuten offline.

## Decisión

**1. Sync incremental por entidad (`GET /api/v1/sync/{entidad}`)** con el mismo patrón de
registro que la búsqueda global (ADR-0096): el core ejecuta **adapters** que registran los packs
(el core no importa de los packs, invariante 5).

- Contrato: `GET /api/v1/sync/{entidad}?cursor=<ISO-8601>&limit=<1..500>` →
  `{entidad, items, nextCursor, deletedIds}`. `items` = filas con marcador > cursor ordenadas por
  marcador; `deletedIds` = ids soft-deleteados después del cursor (solo adapters con soft delete);
  `nextCursor` = marcador máximo devuelto (o el cursor recibido si no hubo filas).
- Cada adapter declara: modelo, sección RBAC (validada con `effective_access`), marcador
  (columna `updated_at` o expresión `COALESCE(…)`), serializador a DTO móvil y si tiene soft
  delete. Registrados en v1: `conversaciones`, `clientes` (core); `clientes_cobranza`, `carteras`,
  `promesas` (cobranza); `productos`, `categorias`, `promociones`, `pedidos` (comercio); `citas`,
  `tipos_cita`, `bloqueos` (calendario).
- Tablas sin `updated_at` recibieron **migración aditiva** (`server_default now()`):
  `pack_cobranza_promesas` (0015), `pack_commerce_categories` y `_promotions` (crm_0008). Las que
  no tienen marca de cambio real usan un marcador documentado: pedidos
  `COALESCE(status_changed_at, created_at)` y conversaciones
  `COALESCE(closed_at, started_at)` (deuda: agregar `updated_at` al core cuando se toquen).
- Resolución de conflictos: last-write-wins por `updated_at` en el cliente; un registro borrado y
  re-creado en la misma ventana puede aparecer en `items` y `deletedIds` (la app aplica el borrado
  al final). Los borrados también avanzan `nextCursor`.
- **Caveat del cursor (documentado):** la paginación es por `marcador > cursor` sin tie-break por
  `id`; filas con el MISMO marcador (solo en inserciones masivas SQL, donde `now()` repite dentro
  de una transacción — los modelos usan `utcnow()` de Python con microsegundos) podrían saltarse
  si un lote las corta. La app puede forzar una re-sync completa de la entidad (cursor inicial)
  cuando detecte huecos.

**2. Auditoría de acciones críticas (`POST /api/v1/audit/mobile-events`)** con tabla de negocio
`mobile_audit_events` (0053, RLS forzada): la app encola `{eventId, accion, entidad, entidadId,
payload, deviceId, ocurridoEn}` y el endpoint responde ack por evento
(`sincronizado`/`duplicado`/`error`) con idempotencia por `(tenant_id, event_id)`, igual criterio
que el sync offline de cobranza.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Un endpoint de sync por módulo (en cada pack) | La app tendría N contratos distintos; el cursor/RBAC se repetiría en cada pack |
| El core consulta tablas de packs | Rompe la invariante 5 |
| Versionado por fila (`version` numérico en cada tabla) | Migrar todas las tablas y educar cada escritura; el cursor por timestamp alcanza con reloj del servidor |
| Auditoría solo en la tabla de cobranza (`_auditoria`) | No cubre acciones de módulos del panel; la tabla core es transversal |

## Consecuencias

**Positivas:** un contrato de sync uniforme con RBAC y RLS; packs extensibles sin tocar el core;
auditoría móvil idempotente para todas las verticales; migraciones solo aditivas.

**Negativas / lo que aceptamos pagar:** marcadores aproximados en conversaciones y pedidos (hasta
agregar `updated_at` al core); sin compresión/delta por campos (la app rehidrata filas completas);
los relojes de los clientes no importan (el cursor usa timestamps del servidor).

**Qué haría que revisáramos esto:** volumen que exija paginación por snapshot/checksum,
necesidad de sync bidireccional con merge de campos, o auditoría con retención legal
(documentada aparte).

## Impacto en el código

- `core/sync/{__init__,providers}.py`, `core/api/routers/{sync,audit}.py`, `core/api/__init__.py`,
  `core/db/models.py` (`MobileAuditEvent`), `alembic/versions/0053_mobile_audit_events.py`.
- Packs: `sync.py` por pack (cobranza/comercio/calendario) + migraciones aditivas
  (cobranza 0015, comercio crm_0008).
- `docs/openapi.json` regenerado.

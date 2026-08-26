# ADR-0004 — Tablas por pack con prefijo; prohibido EAV

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Cada vertical necesita entidades propias (leads, pedidos, citas). Si los packs comparten
tablas genéricas o usan un esquema entity/attribute/value, el modelo de datos se vuelve
ilegible, lento de consultar y caro de migrar; y dos packs pueden colisionar en los nombres.

## Decisión

Cada pack crea sus propias tablas `pack_<pack>_<entidad>` con FK lógica al core, más una
columna `custom jsonb` validada por JSON Schema para el ajuste por tenant. EAV puro
(entity/attribute/value) queda prohibido: destruye rendimiento y legibilidad.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tablas genéricas compartidas | Colisión entre packs; un pack no puede desinstalarse sin tocar el esquema ajeno |
| EAV (entity/attribute/value) | Consultas lentas y modelo ilegible; la historia de los ERPs lo desaconseja |
| Tabla única con columnas dinámicas | Cambios de schema por fila; inviable en Postgres |

## Consecuencias

**Positivas:** aislamiento físico por pack; desinstalar un pack no rompe el core; el
`custom jsonb` cubre el ajuste fino por tenant sin migraciones.

**Negativas / lo que aceptamos pagar:** más tablas en total; cada pack mantiene su propio
árbol Alembic; los nombres de tabla son largos.

**Qué haría que revisáramos esto:** tres packs consecutivos necesitando el mismo par
(prefijo, entidad) o el mismo campo en `custom` — señal de que esa entidad o campo
pertenece al core.

## Impacto en el código

`packages/pack-*` (esquemas propios), `core/db/migrations.py` (rutas de migración por
pack), y el skill `db-migration` (plantilla de prefijo + `custom jsonb`).
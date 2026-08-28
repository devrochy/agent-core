# ADR-0020 — Extracción de pack-commerce desde el plugin heredado mod_crm_engine

- **Estado:** aceptado
- **Fecha:** 2026-08-27

## Contexto

El primer vertical del framework vivía como plugin heredado en `plugins/mod_crm_engine`
con id `mod_crm_engine` y tablas `crm_*`. El Sprint 4 (packs) exige que los verticales
vivan en `packages/pack-*` con enablement por tenant (ADR-0019). Re-hacer las tablas y
re-onboardear CULTO sería destructivo (pedidos, leads y productos en producción).

## Decisión

Extraer en sitio, en dos migraciones coordinadas:

1. **Core `0007`** (corre como dueño, antes que el pack): `plugins.id` y
   `plugin_configs.plugin_id` `mod_crm_engine → pack_commerce` (dropeando y
   re-creando la FK `plugin_configs_plugin_id_fkey`), `agent_specs.spec.packs`
   idem, y el rename de la tabla de versiones alembic
   `alembic_version_mod_crm_engine → alembic_version_pack_commerce` para que el
   árbol del pack continúe desde `crm_0002` sin re-ejecutarse.
2. **Pack `crm_0003`**: `ALTER TABLE ... RENAME` de las 13 tablas `crm_*` →
   `pack_commerce_*` (metadata-only en Postgres: preserva datos, RLS policies,
   índices y grants), `crm_order_counters` idem, secuencia
   `crm_orders_seq → pack_commerce_orders_seq`, y
   `crm_next_order_seq → pack_commerce_next_order_seq`. Se añade `custom jsonb`
   (ADR-0004) en cada tabla de negocio.

El pack pasa a `packages/pack-commerce/` (paquete `pack_commerce`), entry_point
`pack_commerce`, `name = "pack-commerce"`. El stub placeholder `packages/pack-crm`
se elimina.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Crear tablas nuevas y copiar datos | Doble escritura en producción, riesgo de divergencia, y hay que re-numerar |
| Mantener id/tablas `crm_*` | Es deuda que el framework arrastra de por vida y rompe el prefijo `pack_*` (ADR-0004) |
| Migrar el plugin a las 4 costuras de Fase A como `mod_crm_engine` | El renombre es más barato HOY que en un sprint con N tenants |

## Consecuencias

**Positivas:** datos y config de CULTO preservados sin downtime (renames metadata-only);
el pack es 100% del framework (manifest, enablement, playbooks, migraciones propias);
la deuda `mod_crm_engine` del inventory desaparece.

**Negativas / lo que aceptamos pagar:** la migración depende del orden migrate→boot
(0007 primero, para no chocar con `sync_registry`); `pack_commerce_orders.order_seq`
conserva un default heredado de la secuencia global que ya no se usa para numerar
(deuda menor, el UNIQUE por tenant sigue mandando).

**Qué haría que revisáramos esto:** que un pack necesite migraciones que dependan de
datos de otro pack, o que el rename de una tabla de producción en alta disponibilidad
bloquee escrituras (aquí es metadata-only y transaccional).
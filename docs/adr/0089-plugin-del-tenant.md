# ADR-0089 · Plugin del tenant (nivel 3 de personalización)

- **Estado:** aceptado
- **Fecha:** 2026-10-03
- **Relacionado:** [ADR-0001](0001-multitenant-pooled-rls.md) (multi-tenant pooled), [ADR-0004](0004-tablas-por-pack-prefijo-custom-jsonb.md) (tablas por pack con prefijo), [ADR-0019](0019-registry-manifest-enablement-scopes.md) (registry de plugins), [ADR-0024](0024-plugin-health-por-tenant.md) (salud por tenant), invariante nº2 (nunca fork por cliente)

## Contexto

El orden de personalización es **configuración → pack → plugin del tenant →
topología de despliegue** (invariante nº2 de `CLAUDE.md`). Los tres primeros
niveles están formalizados salvo el tercero:

- Configuración: `app_settings` y `AgentSpec` por tenant.
- Pack: paquete instalable en `packages/pack-*`, multi-tenant, sirve a varios
  negocios (pack-commerce, pack-cobranza).
- **Plugin del tenant:** un paquete instalable **acotado a UN solo negocio**,
  para una necesidad particular que no justifica un pack ni tolera un fork.

Hoy el scaffold (`agent-core plugin scaffold`, `cli/commands/plugin.py`) genera
un plugin con `pyproject.toml`, `__init__.py`, `config.py`, `skills.py` y
`module.py` — **sin** `models.py`, `jobs.py`, migraciones ni tests. Un plugin del
tenant que necesite datos o tareas propias no tiene un camino soportado: termina
en un fork o en tools declarativas que no alcanzan.

## Decisión

Se formaliza el **nivel 3 "plugin del tenant"** como un **paquete instalable
acotado a un negocio**, con poder completo (tablas, cron, config, skills) y las
mismas garantías del core:

1. **Mismo binario (invariante nº2).** El plugin es paquete Python con
   entry_point en `agent_core.plugins`; no hay fork del core ni del runtime.
   Se instala con `agent-core plugin install` o `pip install -e`.
2. **Aislamiento obligatorio (invariante nº1).** Toda tabla del plugin lleva
   `tenant_id uuid not null`, RLS **habilitada y forzada** con policy
   `tenant_isolation` (`using` + `with check`) e índice compuesto que empieza por
   `tenant_id`. El scaffold **genera el modelo y la migración con esta forma**, y
   el hook/guarda de migraciones la exige (misma regla que core y packs).
3. **Activación por tenant.** El plugin se habilita para el negocio dueño a
   través del AgentSpec/registry (ADR-0019); sus cron jobs usan el decorador
   `@register_cron`, que salta si el plugin está inactivo para el tenant.
4. **Migraciones propias.** Árbol Alembic con `version_table` distinta
   (`alembic_version_<plugin_id>`) para no chocar con el core ni con packs.
   Las corre el `plugin_manager` al activar (`core.db.migrations.upgrade_plugin`).
5. **Revisión del master.** Un plugin del tenant es código de negocio con RLS:
   pasa por el `auditor-multitenant` y los checks de invariantes (guarda de
   migraciones + `ruff` + `pytest`) antes de desplegarse. El scaffold incluye un
   test que verifica `tenant_id` + RLS en su migración, y el plugin de ejemplo
   se entrega con esa forma.
6. **Scaffold completo.** `agent-core plugin scaffold <nombre> --tenant` genera:
   `pyproject.toml`, `__init__.py`, `config.py`, `skills.py`, `module.py`,
   **`models.py`** (con `TenantMixin` y `deleted_at`), **`jobs.py`**,
   **`alembic.ini` + `alembic/env.py` + `alembic/versions/0001_*.py`** (tabla de
   ejemplo con RLS) y **`tests/test_<plugin>_migracion_rls.py`**.

## Consecuencias

- Un caso particular de un negocio (una tabla, un cron, una tool con datos
  propios) se resuelve sin fork ni código en el core.
- La personalización sigue siendo *configuración* + *paquete instalable*, nunca
  un binario distinto: los checks de invariantes son los mismos que core/packs.
- Coste: cada plugin del tenant requiere revisión humana (es código con RLS).
  Es deliberado: la alternativa (sandbox de funciones del cliente, opción B de
  [SPEC-22](../../../solvershub-docs/05-fase-2-plus/SPEC-22-extensibilidad-avanzada/spec.md))
  abre una superficie de seguridad nueva y costosa de operar.
- El scaffold con `--tenant` no cambia el comportamiento por defecto
  (`plugin scaffold <nombre>` sigue generando el pack multi-tenant).

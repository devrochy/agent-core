# Auditoría de aislamiento multi-tenant — inicial

**Fecha:** 2026-08-26
**Estado:** Sprint 0 · runtime single-tenant de diseño
**Criterio:** ADR-0001 (multi-tenant pooled + RLS) e invariantes de CLAUDE.md

**Nota de contexto:** el runtime actual es single-tenant de diseño (`core/db/models.py:46`),
por lo que *hoy* no existe una fuga activa entre tenants reales. Sin embargo, sí hay
hallazgos de severidad alta en el sentido de "fuga posible entre tenants": el esquema
completo y el modelo de acceso a datos carecen por diseño de cualquier dimensión de tenant
(`tenant_id`, RLS, `ctx.db`), lo que viola el invariante nº 1 y hace inviable la migración
pooled sin re-arquitectura. Esos hallazgos son la base sobre la que cualquier pooling futuro
filtraría datos entre tenants.

## Hallazgos

| severidad | archivo:línea | problema | corrección sugerida |
|---|---|---|---|
| **alta** | `core/db/models.py:43-249` (todas las tablas) | Ninguna tabla de negocio tiene columna `tenant_id`; modelo single-tenant de diseño. Al migrar a pooled no hay columna sobre la que filtrar ni índice compuesto por tenant. | Añadir `tenant_id uuid not null` + índice compuesto `(tenant_id, ...)` a cada tabla de negocio (customers, conversations, messages, app_settings, escalations, llm_usage, audit_log, memory_vectors, channel_connections). |
| **alta** | `alembic/versions/0001_initial_schema.py:29-203` | Ninguna migración habilita RLS ni define policies; no existe `tenant_id` en ningún `create_table`. | Migración nueva: `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + policy `USING (tenant_id = current_setting('app.tenant_id'))` por tabla. |
| **alta** | `plugins/pack_commerce/alembic/versions/0001_crm_full.py:25-199` | Las tablas `crm_*` se crean sin `tenant_id`, sin RLS ni policies. | Añadir `tenant_id` + RLS/policy + índice a todas las tablas `crm_*`. |
| **alta** | `plugins/pack_commerce/.../skills.py:19,94,127,368,631` · `module.py:25,81,103,142` · `jobs.py:17,47,102` | Plugins acceden a la BD por `get_session()` (pool crudo) en vez del `ToolContext.db` scoped del contrato. Sin filtro de tenant en ninguna query. | Migrar plugins a `ToolContext.db` (RLS-filtered); eliminar `get_session()` del runtime de tools. |
| **alta** | `core/mcp/types.py:38-49` · `core/mcp/host.py:152-160` | El `ToolContext` del runtime NO lleva `tenant_id` ni `db`/`secrets`/`http` scoped, divergiendo del contrato (`contracts.py:274-285`). Un plugin no puede ser scopeado por tenant. | Alinear `ToolContext` del runtime con el contrato. |
| **media** | `core/plugin_manager.py:46-47,84,130-141,144-156` | Singleton `plugin_manager` guarda una instancia por plugin con `module.config` cargado por `plugin_id` sin tenant (`load_config` L134-135). En pooled, la config de un tenant se comparte con todos. | Config keyed por `(tenant_id, plugin_id)`; el plugin no guarda config mutable en la instancia compartida. |
| **media** | `core/mcp/host.py:149` · `core/shared/events.py:76` | `mcp_host` y `event_bus` son singletons globales; un suscriptor de un tenant procesaría eventos de otro en pooled. | Scopeo de registros/suscriptores por tenant o agente. |
| **media** | `core/orchestrator/debounce.py:108-118` | SQL crudo que recorre `messages`/`conversations` sin filtro de tenant ni `ctx.db`. | Reescribir con ORM + filtro de tenant. |
| **media** | `core/mcp/types.py:53-58` · `core/mcp/translator.py:14-18` | El `MCPTool` del runtime no declara ni aplica `scopes`/`sideEffect` (el contrato sí). Los tools del CRM acceden a toda la tabla sin restricción por scope. | Implementar `scopes` y hacer cumplir `ToolBinding` del AgentSpec. |
| **media** | `plugins/pack_calendar/.../skills.py:14,85,108` · `oauth.py:27-28,51,117` · `core/llm/vector.py:55` | Llamadas de red a `*.googleapis.com`, `accounts.google.com`, `api.openai.com` sin manifest `agentpkg.yaml` que declare la allowlist (no existe ningún manifest). | Crear manifests por pack con allowlist de dominios. |
| **baja** | `core/memory/vector.py:64-65` | Singletons `_provider`/`_provider_resolved` (proveedor de embeddings) compartidos entre requests; no retiene datos de tenant pero es estado de módulo. | Documentar; por-tenant si se aíslan claves API. |

## Sin hallazgos

- **Service role key (punto 3):** no hay uso en código.
- **`os.environ` en plugins (punto 4):** no hay; los plugins solo leen vía `get_settings()`. Usos en `cli/`/`tests/`, fuera del runtime de plugins.
- **`tenant_id` desde el body (punto 7):** no se lee; el webhook usa `phone_number_id` solo para ruteo de credenciales.
- **PII en logs (punto 9):** 52 call-sites revisados; ningún log imprime contenido de mensajes ni datos personales.

## Punto más significativo (el que más se escapa)

El runtime está lleno de **singletons de módulo** (`plugin_manager`, `mcp_host`, `event_bus`,
`_box`, `_provider`) cuyo estado (`_modules`, `_active`, `_subscribers`, `module.config`) es
global al proceso y no keyed por tenant. Hoy no retiene datos de tenant (hay un solo tenant
de diseño), pero es el patrón que filtrará estado entre tenants cuando se active el pooling
del ADR-0001.

## Siguiente paso

Sprint 1 (ROADMAP): `tenant_id` + RLS en todo el esquema, `ToolContext` del runtime alineado
al contrato (tenantId + ScopedDb), plugins sin `get_session()`, config de plugin por tenant.
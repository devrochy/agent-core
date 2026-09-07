# Inventario de código heredado

Generado con `/prompts/00-inventario.md`. **Revisado a mano por Rob:** [x] no / [ ] sí

Este fichero es la autoridad para el subagente `migrador`. Si algo no está aquí, el
migrador debe detenerse y preguntar.

Nota de contexto: el runtime real es **Python** (ADR-0011). La estructura objetivo es la de
`CLAUDE.md`, que ya refleja Python. `@agentcore/contracts` se está migrando de TS a Python.

## Resumen

| Categoría | % líneas | Interpretación |
|---|---|---|
| CORE | 64,1% | reutilizable entre verticales |
| PACK | 19,7% | específico de un nicho |
| CONFIG | 3,4% | debería ser dato, no código |
| APP | 12,8% | UI y rutas del panel |

**14.675 líneas / 184 archivos** clasificados (excluye `.venv`, `node_modules`, `__pycache__`,
`*.egg-info`).

Si CORE < 40%, el primer trabajo es extracción, no features nuevas.

CORE ≥ 40%, pero ojo: la mayor parte del "CORE" (runtime, esquema, migraciones, panel) es
el **data-layer single-tenant heredado**, que hay que reescribir a `tenant_id` + RLS antes
de poder extraer nada. El primer trabajo no es extracción: es **hacer multi-tenant el
esquema** (Sprint 1 del ROADMAP).

## Clasificación

| Ruta | Categoría | Destino | Riesgo | Notas |
|---|---|---|---|---|
| `packages/contracts/package.json` | CORE | `packages/contracts` | bajo | Manifest `@agentcore/contracts` v1.0.0, sin deps. En TS → migrar a Python (ADR-0011) |
| `packages/contracts/src/index.ts` | CORE | `packages/contracts` (Python) | bajo | 5 contratos congelados completos (ADR-0003→0011). Sin prompts en `.ts` |
| `alembic/env.py` | CORE | `alembic/` | medio | Sin bootstrap de RLS; hay que enchufar policies al migrar |
| `alembic/script.py.mako` | CORE | `alembic/` | bajo | Plantilla sin boilerplate `tenant_id`/RLS |
| `alembic/versions/0001_initial_schema.py` | CORE | `alembic/versions/` (reescribir) | **alto** | 13 tablas sin `tenant_id`, sin RLS, sin `deleted_at`, sin índice por tenant |
| `alembic/versions/0002_message_status.py` | CORE | `alembic/versions/` | **alto** | Correcto en sí, arrastra base sin tenant_id |
| `alembic.ini` | CORE | raíz | bajo | Infraestructura |
| `core/__init__.py` | CORE | `packages/core-runtime` | bajo | Marcador + `__version__` |
| `core/config.py` | CONFIG | `.env` + AgentSpec | bajo | Knobs env; `DEBOUNCE_DELAY_MS` es comportamiento por cliente; infra se queda |
| `core/db/__init__.py` | CORE | `packages/core-runtime/db` | bajo | Marcador |
| `core/db/migrations.py` | CORE | `packages/core-runtime` | bajo | Ruta core+plugins, tabla de versión por plugin |
| `core/db/models.py` | CORE | `packages/core-runtime/db` | **alto** | 13 tablas de negocio sin `tenant_id`/RLS; AppSettings singleton (single-tenant de diseño) |
| `core/db/session.py` | CORE | `packages/core-runtime/db` | **alto** | Engine global; falta `SET LOCAL role`/RLS por request |
| `core/gateway/__init__.py` | CORE | `packages/core-runtime/gateway` | bajo | Marcador |
| `core/gateway/channels/__init__.py` | CORE | `packages/core-runtime/gateway` | bajo | Marcador |
| `core/gateway/channels/base.py` | CORE | `packages/contracts` | bajo | Contrato de adaptadores in/out |
| `core/gateway/channels/registry.py` | CORE | `packages/core-runtime/gateway` | bajo | Registry de adaptadores |
| `core/gateway/channels/whatsapp/__init__.py` | PACK | `packages/channel-whatsapp` | bajo | Marcador |
| `core/gateway/channels/whatsapp/inbound.py` | PACK | `packages/channel-whatsapp` | bajo | Parse + verificación HMAC Meta |
| `core/gateway/channels/whatsapp/outbound.py` | PACK | `packages/channel-whatsapp` | bajo | Envío Meta Graph API |
| `core/gateway/connections.py` | CORE | `packages/core-runtime/gateway` | medio | `CREDENTIAL_KEYS`/`verify_meta_credentials` (Meta) → channel-whatsapp; queries sin tenant |
| `core/gateway/idempotency.py` | CORE | `packages/core-runtime/gateway` | bajo | Dedupe Redis SET NX |
| `core/gateway/message_splitter.py` | CORE | `packages/core-runtime/gateway` | bajo | Split de burbujas; estilos → CONFIG |
| `core/gateway/sender.py` | CORE | `packages/core-runtime/gateway` | medio | Queries sin tenant; `BUBBLE_DELAY_S`/`SEND_RETRIES` → CONFIG |
| `core/gateway/webhooks.py` | CORE | `apps/gateway` | medio | Ref dura a `mod_media_processor` (:120-132); fallback de texto |
| `core/llm/__init__.py` | CORE | `packages/core-runtime/llm` | medio | `base_urls` hardcodeadas; deepseek-default es decisión de config |
| `core/llm/base.py` | CORE | `packages/core-runtime/llm` | bajo | Puerto LLM ABC |
| `core/llm/types.py` | CORE | `packages/contracts` | bajo | Contrato neutral de mensajes |
| `core/llm/anthropic_provider.py` | CORE | `packages/core-runtime/llm` | medio | Tabla PRICING hardcodeada → CONFIG (costos desactualizables) |
| `core/llm/openai_compatible.py` | CORE | `packages/core-runtime/llm` | medio | Tabla PRICING hardcodeada → CONFIG |
| `core/llm/prompt_builder.py` | CORE | estructura → core-runtime; contenido → AgentSpec/SKILL.md | **alto** | Bloque ensamblador genérico, pero `TONE_BLOCKS` + `BASE_RULES` (regla CULTO en :47-48) hardcodeados |
| `core/llm/difficulty_router.py` | CORE | `packages/core-runtime/llm` | medio | Regex español + umbrales → CONFIG por tenant |
| `core/main.py` | CORE | `apps/gateway` (shell monolith) | bajo | Ensamblaje FastAPI |
| `core/mcp/__init__.py` | CORE | `packages/core-runtime/mcp` | bajo | Marcador |
| `core/mcp/host.py` | CORE | `packages/core-runtime/mcp` | bajo | CORE fuerte: registry + dispatch + aislamiento de plugins |
| `core/mcp/translator.py` | CORE | `packages/core-runtime/mcp` | bajo | Traducción MCP→LLM |
| `core/mcp/types.py` | CORE | `packages/contracts` | bajo | `ToolContext` sin pool/env ✓ (cumple invariante #6) |
| `core/memory/__init__.py` | CORE | `packages/core-runtime/memory` | bajo | Marcador |
| `core/memory/entity.py` | CORE | `packages/core-runtime/memory` | medio | `find_by_external_id` sin filtro tenant |
| `core/memory/vector.py` | CORE | `packages/core-runtime/memory` | medio | Recall por `customer_id` solo, sin `tenant_id` (fuga cross-tenant si pooled) |
| `core/memory/working.py` | CORE | `packages/core-runtime/memory` | bajo | Memoria Redis, genérica |
| `core/orchestrator/__init__.py` | CORE | `packages/core-runtime/orchestrator` | bajo | Marcador |
| `core/orchestrator/consumer.py` | CORE | `apps/worker` | **alto** | Queries sin tenant (AppSettings, Customer, Connection, Conversation); kill-switch |
| `core/orchestrator/debounce.py` | CORE | `apps/worker` | medio | Raw SQL `recover_orphaned_conversations` sin tenant |
| `core/orchestrator/escalation.py` | CORE | `packages/core-runtime` | medio | **CORE fuerte** (máquina de escalamiento genérica); keywords español + `ESCALATION_REPLY` → AgentSpec |
| `core/orchestrator/guardrails.py` | CORE | `packages/core-runtime` | medio | Runner fail-open; `retry_instruction` → AgentSpec |
| `core/orchestrator/loop.py` | CORE | `packages/core-runtime` | **alto** | Queries sin tenant; `FALLBACK_REPLY`, tool-error, retry → AgentSpec |
| `core/orchestrator/safe_renderer.py` | CORE | `packages/core-runtime` | bajo | **CORE fuerte**: inyección determinística de datos sensibles |
| `core/orchestrator/skill_router.py` | CORE | `packages/core-runtime` | bajo | Relevancia keyword→skill |
| `core/plugin_manager.py` | CORE | `packages/core-runtime` | medio | Lifecycle de plugins; muta `sys.path` (dev-only) |
| `core/shared/__init__.py` | CORE | `packages/core-runtime` | bajo | Marcador |
| `core/shared/audit.py` | CORE | `packages/core-runtime` | bajo | **CORE fuerte**: ledger append-only inmutable |
| `core/shared/crypto.py` | CORE | `packages/core-runtime` | bajo | Fernet |
| `core/shared/events.py` | CORE | `packages/core-runtime` | bajo | Bus in-process |
| `core/shared/exceptions.py` | CORE | `packages/contracts` | bajo | Jerarquía de excepciones |
| `core/shared/logger.py` | CORE | `packages/core-runtime` | bajo | structlog JSON |
| `core/tasks/__init__.py` | CORE | `packages/core-runtime` | bajo | Marcador |
| `core/tasks/celery_app.py` | CORE | `apps/worker` | bajo | Wiring Celery |
| `core/tasks/decorators.py` | CORE | `packages/core-runtime` | bajo | `@register_cron` para plugins |
| `core/tasks/queue.py` | CORE | `packages/core-runtime` | bajo | Puerto TaskQueue |
| `core/admin/__init__.py` | APP | `apps/agentos` | bajo | Marcador |
| `core/admin/auth.py` | APP | `apps/agentos` | bajo | Sesión panel (cookie + bcrypt) |
| `core/admin/form_generator.py` | APP | `apps/agentos` | bajo | Pydantic→Bootstrap |
| `core/admin/views.py` | APP | `apps/agentos` | medio | Queries del panel **sin filtro tenant** (lee todas las filas) |
| `core/admin/templates/base.html` | APP | `apps/agentos` | bajo | Layout |
| `core/admin/templates/conexiones.html` | APP | `apps/agentos` | bajo | UI conexión WhatsApp |
| `core/admin/templates/conversation_detail.html` | APP | `apps/agentos` | bajo | Hilo + respuesta humana |
| `core/admin/templates/conversations.html` | APP | `apps/agentos` | bajo | Lista |
| `core/admin/templates/costs.html` | APP | `apps/agentos` | bajo | Costos LLM |
| `core/admin/templates/dashboard.html` | APP | `apps/agentos` | bajo | KPIs |
| `core/admin/templates/escalations.html` | APP | `apps/agentos` | bajo | Cola de escalamientos |
| `core/admin/templates/login.html` | APP | `apps/agentos` | bajo | Login |
| `core/admin/templates/plugin_config.html` | APP | `apps/agentos` | bajo | Shell config de plugin |
| `core/admin/templates/plugins.html` | APP | `apps/agentos` | bajo | Lista/activar plugins |
| `core/admin/templates/settings.html` | APP | `apps/agentos` | bajo | Aquí vivirá la edición de AgentSpec |
| `cli/main.py` | CORE | `cli/` | bajo | Entry point Typer |
| `cli/__init__.py` | CORE | `cli/` | bajo | Marcador |
| `cli/registry.py` | CORE | `cli/` | bajo | Resolución install; `KNOWN_PLUGINS` lista hardcodeada (config-ish) |
| `cli/sdd_generator.py` | CORE | `cli/` | bajo | Genera docs SDD |
| `cli/preflight.py` | CORE | `cli/` | bajo | `LLM_DEFAULTS` embebidos (config en CORE) |
| `cli/commands/__init__.py` | CORE | `cli/` | bajo | Marcador |
| `cli/commands/init.py` | CORE | `cli/` | bajo | Wizard bootstrap; `ENV_TEMPLATE` con defaults (config embebida) |
| `cli/commands/configure.py` | CORE | `cli/` | bajo | Reconfigura tono/LLM |
| `cli/commands/db.py` | CORE | `cli/` | bajo | Migraciones core+plugins |
| `cli/commands/doctor.py` | CORE | `cli/` | bajo | Diagnóstico |
| `cli/commands/plugin.py` | CORE | `cli/` | bajo | install/remove/scaffold; scaffold genera plantillas (no runtime) |
| `cli/commands/sdd.py` | CORE | `cli/` | bajo | Comandos SDD |
| `cli/wizards/__init__.py` | CORE | `cli/` | bajo | Marcador |
| `cli/wizards/db_import_wizard.py` | CORE | `cli/` | **alto** | Genera `INSERT ... SELECT` **sin `tenant_id`** |
| `scripts/init_db.py` | CORE | `scripts/` | bajo | Seed admin/config; `assistant_name` default hardcodeado |
| `scripts/entrypoint.sh` | CORE | `scripts/` | bajo | Entrypoint contenedor |
| `scripts/install_plugins.sh` | CORE | `scripts/` | bajo | Dev tooling |
| `plugins/pack_commerce/pack_commerce/__init__.py` | PACK | `packages/pack-crm` | bajo | Marcador |
| `plugins/pack_commerce/pack_commerce/module.py` | PACK | `packages/pack-crm` | **alto** | Rutas admin `/crm/*` sin filtro tenant; guardrail `retry_instruction` hardcodeado |
| `plugins/pack_commerce/pack_commerce/models.py` | PACK | `packages/pack-crm` | **alto** | 13 tablas `crm_*` sin `tenant_id`/RLS |
| `plugins/pack_commerce/pack_commerce/config.py` | CONFIG | config por tenant | bajo | Umbrales (monto_alto, inactividad) y moneda |
| `plugins/pack_commerce/pack_commerce/jobs.py` | PACK | `packages/pack-crm` | **alto** | Crons `follow_up_*` barren leads/cotizaciones de **todos** los tenants; mensajes hardcodeados |
| `plugins/pack_commerce/pack_commerce/skills.py` | PACK | `packages/pack-crm` | **alto** | ~25 queries sin `tenant_id`; 10 tool-descriptions procedurales; copy al cliente hardcodeado; moneda COP fija (:668) |
| `plugins/pack_commerce/pack_commerce/templates/crm/dashboard.html` | APP | panel del pack | medio | UI sin filtro tenant (hereda de module.py) |
| `plugins/pack_commerce/pack_commerce/templates/crm/leads.html` | APP | panel del pack | medio | UI leads |
| `plugins/pack_commerce/pack_commerce/templates/crm/categorias.html` | APP | panel del pack | medio | UI categorías |
| `plugins/pack_commerce/pack_commerce/templates/crm/_category_node.html` | APP | panel del pack | bajo | Partial |
| `plugins/pack_commerce/pack_commerce/templates/crm/pedidos.html` | APP | panel del pack | medio | UI pedidos |
| `plugins/pack_commerce/pack_commerce/templates/crm/productos.html` | APP | panel del pack | medio | UI productos + import CSV |
| `plugins/pack_commerce/pack_commerce/templates/crm/promociones.html` | APP | panel del pack | medio | UI promociones |
| `plugins/pack_commerce/alembic/env.py` | PACK | `packages/pack-crm` | medio | Árbol alembic propio; lee `database_url` |
| `plugins/pack_commerce/alembic.ini` | PACK | `packages/pack-crm` | bajo | Config alembic |
| `plugins/pack_commerce/alembic/script.py.mako` | PACK | `packages/pack-crm` | bajo | Plantilla |
| `plugins/pack_commerce/alembic/versions/0001_crm_full.py` | PACK | `packages/pack-crm` | **alto** | 13 tablas `crm_*` sin `tenant_id`/RLS; `public_order_number` 'OC-' hardcodeado |
| `plugins/pack_commerce/pyproject.toml` | PACK | `packages/pack-crm` | bajo | Manifest + entidades para wizard |
| `plugins/pack_calendar/pack_calendar/__init__.py` | PACK | `packages/pack-calendar` | bajo | Marcador |
| `plugins/pack_calendar/pack_calendar/module.py` | PACK | `packages/pack-calendar` | medio | Rutas OAuth; sin tablas propias |
| `plugins/pack_calendar/pack_calendar/skills.py` | PACK | `packages/pack-calendar` | medio | 2 tool-descriptions; copy cita hardcodeado |
| `plugins/pack_calendar/pack_calendar/oauth.py` | PACK | `packages/pack-calendar` | medio | Flujo OAuth; tokens en `plugin_configs` |
| `plugins/pack_calendar/pack_calendar/config.py` | CONFIG | config por tenant | bajo | `client_id/secret`, `calendar_id`, `timezone` |
| `plugins/pack_calendar/pyproject.toml` | PACK | `packages/pack-calendar` | bajo | Metadata |
| `tests/conftest.py` | CORE | `tests/` | bajo | Fixtures |
| `tests/test_compose_identity.py` | CORE | `tests/` | medio | Testea topología "un despliegue = un tenant" (dedicado), no pooled |
| `tests/test_docker_errors.py` | CORE | `tests/` | bajo | Errores Docker |
| `tests/test_env_template.py` | CORE | `tests/` | medio | "Barbería El Corte" hardcodeada (:54) |
| `tests/test_escalation.py` | CORE | `tests/` | medio | Depende de `DEFAULT_ESCALATION_KEYWORDS` |
| `tests/test_message_splitter.py` | CORE | `tests/` | bajo | Lógica pura |
| `tests/test_migrations_path.py` | CORE | `tests/` | bajo | Acopla `pack_commerce` |
| `tests/test_port_detection.py` | CORE | `tests/` | bajo | Puro |
| `tests/test_safe_renderer.py` | CORE | `tests/` | bajo | SafeRenderer |
| `evals/runner.py` | CORE | `evals/` | medio | Identidad de prueba hardcodeada (:54-55) |
| `evals/golden_scenarios/01_happy_path.json` | CORE | `evals/` | bajo | Genérico |
| `evals/golden_scenarios/02_tool_calling.json` | PACK | `packages/pack-crm/evals/` | medio | Nombra `pack_commerce`/`crear_lead`; vive en set global, debería estar en el pack |
| `evals/golden_scenarios/03_prompt_injection.json` | CORE | `evals/` | bajo | Seguridad |
| `evals/golden_scenarios/04_out_of_scope.json` | CORE | `evals/` | bajo | Genérico |
| `evals/golden_scenarios/05_escalation.json` | CORE | `evals/` | bajo | Usa keyword "asesor" (debería venir de AgentSpec) |
| `sdd/architecture.md` | CONFIG | despliegue del tenant | bajo | Generado |
| `sdd/changelog.md` | CONFIG | despliegue del tenant | bajo | Generado |
| `sdd/channels.md` | CONFIG | despliegue del tenant | bajo | Generado |
| `sdd/entities.md` | CONFIG | despliegue del tenant | bajo | Generado |
| `sdd/project.md` | CONFIG | despliegue del tenant | bajo | Identidad del tenant |
| `sdd/skills-config.md` | CONFIG | despliegue del tenant | medio | Referencia `BASE_RULES` de prompt_builder |
| `sdd/skills.md` | CONFIG | despliegue del tenant | bajo | Generado |
| `sdd/.state.json` | CONFIG | gitignored (debería serlo) | medio | **Datos reales de tenant en git** ("Barbería El Corte") |
| `sdd/templates/architecture.md.j2` | CORE | `sdd/templates/` | bajo | Plantilla de generación |
| `sdd/templates/changelog.md.j2` | CORE | `sdd/templates/` | bajo | Plantilla |
| `sdd/templates/channels.md.j2` | CORE | `sdd/templates/` | bajo | Plantilla |
| `sdd/templates/entities.md.j2` | CORE | `sdd/templates/` | bajo | Plantilla |
| `sdd/templates/project.md.j2` | CORE | `sdd/templates/` | bajo | Plantilla |
| `sdd/templates/skills-config.md.j2` | CORE | `sdd/templates/` | medio | "Reglas INVIOLABLES" procedimiento embebido |
| `sdd/templates/skills.md.j2` | CORE | `sdd/templates/` | bajo | Plantilla |
| `.opencode/agents/arquitecto.md` | CORE | `.opencode/agents/` | bajo | Dev tooling |
| `.opencode/agents/auditor-multitenant.md` | CORE | `.opencode/agents/` | bajo | Auditor aislamiento |
| `.opencode/agents/customizer.md` | CORE | `.opencode/agents/` | bajo | Personalizador |
| `.opencode/agents/eval-runner.md` | CORE | `.opencode/agents/` | bajo | Runner pre-release |
| `.opencode/agents/migrador.md` | CORE | `.opencode/agents/` | bajo | Migración legacy |
| `.opencode/agents/migrator.md` | CORE | `.opencode/agents/` | bajo | Migración de datos |
| `.opencode/agents/optimizer.md` | CORE | `.opencode/agents/` | bajo | Costo LLM |
| `.opencode/agents/plugin-dev.md` | CORE | `.opencode/agents/` | bajo | Scaffolding plugins |
| `.opencode/skills/adr/SKILL.md` | CORE | `.opencode/skills/` | bajo | Procedimiento ADR |
| `.opencode/skills/agentcore-conventions/SKILL.md` | CORE | `.opencode/skills/` | bajo | Convenciones de capa |
| `.opencode/skills/db-migration/SKILL.md` | CORE | `.opencode/skills/` | bajo | Norma que el alembic actual viola |
| `.opencode/skills/eval-author/SKILL.md` | CORE | `.opencode/skills/` | bajo | Golden scenarios |
| `.opencode/skills/incident/SKILL.md` | CORE | `.opencode/skills/` | bajo | Runbook |
| `.opencode/skills/new-pack/SKILL.md` | CORE | `.opencode/skills/` | bajo | Scaffolding packs |
| `.opencode/skills/new-tool/SKILL.md` | CORE | `.opencode/skills/` | bajo | Scaffolding tools |
| `.opencode/skills/onboard-tenant/SKILL.md` | CORE | `.opencode/skills/` | bajo | Onboarding tenants |
| `.opencode/skills/release/SKILL.md` | CORE | `.opencode/skills/` | bajo | Checklist release |
| `.opencode/plugins/guards.ts` | CORE | `.opencode/plugins/` | bajo | Guard que bloquea migraciones sin RLS/tenant_id |
| `.opencode/package.json` | CORE | `.opencode/` | bajo | Dep plugin (gitignored) |
| `.opencode/package-lock.json` | CORE | `.opencode/` | bajo | Lockfile (gitignored) |
| `.opencode/.gitignore` | CORE | `.opencode/` | bajo | Excluye manifests |
| `pyproject.toml` | CORE | raíz | bajo | Build/deps/lint/test |
| `Makefile` | CORE | raíz | bajo | Objetivos dev |
| `Dockerfile` | CORE | raíz | bajo | Imagen |
| `docker-compose.yml` | CORE | raíz | medio | Topología "dedicado" (un stack = un tenant); el default pooled requiere compose distinto |
| `.env.example` | CONFIG | raíz (plantilla por tenant) | bajo | Exactamente lo que el cliente edita |
| `README.md` | CORE | raíz | medio | Reescribido para el modelo multi-tenant (ADR-0001): instalar una vez, dar de alta tenants |
| `README-BOOTSTRAP.md` | CORE | raíz | bajo | Kit de arranque (untracked) |
| `opencode.json` | CORE | raíz | bajo | Permisos OpenCode (untracked) |
| `.gitignore` | CORE | raíz | bajo | Ignora `sdd/*.md` pero NO `sdd/.state.json` |
| `LICENSE` | CORE | raíz | bajo | MIT |
| `prompts/00-inventario.md` | CORE | `prompts/` (transitorio) | bajo | Prompt de flujo |
| `prompts/01-sprint-0.md` | CORE | `prompts/` (transitorio) | bajo | Fase 2 |
| `prompts/02-sprint-1.md` | CORE | `prompts/` (transitorio) | bajo | Fase 3 |
| `prompts/03-plantilla-sprint.md` | CORE | `prompts/` (transitorio) | bajo | Plantilla |

## Prompts hardcodeados encontrados

| Ruta | Tipo (identidad / procedimiento) | Destino (AgentSpec / SKILL.md) |
|---|---|---|
| `core/llm/prompt_builder.py:20-26` — `TONE_BLOCKS["calido"]` | identidad | AgentSpec |
| `core/llm/prompt_builder.py:27-31` — `TONE_BLOCKS["formal"]` | identidad | AgentSpec |
| `core/llm/prompt_builder.py:32-37` — `TONE_BLOCKS["divertido"]` | identidad | AgentSpec |
| `core/llm/prompt_builder.py:40-52` — `BASE_RULES` | procedimiento (regla CULTO de lead en :47-48) | SKILL.md de pack-crm + AgentSpec |
| `core/llm/prompt_builder.py:73` — "Eres {assistant_name}…" | identidad | AgentSpec |
| `core/orchestrator/escalation.py:25` — `DEFAULT_ESCALATION_KEYWORDS` | procedimiento | AgentSpec (config por tenant) |
| `core/orchestrator/escalation.py:27-30` — `ESCALATION_REPLY` | identidad | AgentSpec |
| `core/orchestrator/loop.py:50-53` — `FALLBACK_REPLY` | identidad | AgentSpec |
| `core/orchestrator/loop.py:194-196` — tool-error | identidad | AgentSpec |
| `core/orchestrator/guardrails.py:41-42` — `retry_instruction` | procedimiento | AgentSpec |
| `core/gateway/webhooks.py:125` — fallback multimedia | identidad | AgentSpec |
| `plugins/pack_commerce/pack_commerce/skills.py:199-582` — 10 descripciones de tools (crear/calificar lead, inventario, cotización, promoción, pedido, estado) | procedimiento | SKILL.md de pack-crm |
| `plugins/pack_commerce/pack_commerce/skills.py:928-940` — confirmación de pedido `_safe_append` | copy al cliente | CONFIG (lo cambia el cliente solo) |
| `plugins/pack_commerce/pack_commerce/skills.py:40-54` — `ORDER_STATUS_VISIBLE`/`LABEL` | copy al cliente | CONFIG |
| `plugins/pack_commerce/pack_commerce/skills.py:668` — moneda `COP` fija | CONFIG | CRMConfig.currency_code (ya existe) |
| `plugins/pack_commerce/pack_commerce/module.py:595-598` — `retry_instruction` guardrail | procedimiento | SKILL.md de pack-crm |
| `plugins/pack_commerce/pack_commerce/jobs.py:24-31` — `FOLLOW_UP_LEAD/QUOTE_MESSAGE` | procedimiento + copy | SKILL.md de pack-crm + CONFIG |
| `plugins/pack_calendar/pack_calendar/skills.py:31-51` — 2 descripciones de tools | procedimiento | SKILL.md de pack-calendar |
| `plugins/pack_calendar/pack_calendar/skills.py:98` — "Tu cita quedó agendada…" | copy al cliente | CONFIG |
| `evals/runner.py:54-55` — identidad de prueba fija | identidad | AgentSpec / fixture de escenario |

**Cero prompts en `.ts`** (contracts y guards.ts limpios). Todos los encontrados viven en
`.py`. Los `.opencode/agents/*.md` y `skills/*/SKILL.md` son prompts de desarrollo (devkit),
no del agente del tenant: no violan la invariante #3.

**Objetivo: que esta tabla quede vacía al final del Sprint 3.**

## Riesgos de aislamiento (invariante #1 y #6)

- El esquema entero (core y CRM) es **single-tenant de diseño**: ninguna de las 26 tablas de
  negocio tiene `tenant_id` ni RLS. Contradice ADR-0001. Ver `core/db/models.py`,
  `alembic/versions/0001_initial_schema.py`, `plugins/pack_commerce/.../models.py`,
  `.../alembic/versions/0001_crm_full.py`.
- `core/db/session.py` no configura role/RLS por tenant en pooled.
- `core/orchestrator/consumer.py`, `core/orchestrator/loop.py`, `core/admin/views.py`,
  `core/gateway/connections.py`, `core/gateway/sender.py`, `core/memory/entity.py`,
  `core/memory/vector.py` — queries que asumen un solo tenant.
- `plugins/pack_commerce/.../jobs.py` — crons de follow-up barren datos de **todos** los
  tenants y envían WhatsApp: fuga cross-tenant directa.
- `cli/wizards/db_import_wizard.py` — genera `INSERT ... SELECT` sin `tenant_id`.
- Invariante #6 (plugins solo `ToolContext`): hoy los plugins reciben `get_session()` y
  `get_settings()`. Tensión arquitectónica a resolver (documentada, no bloqueante).

## Top 10 — mayor riesgo de migración

1. **`core/db/models.py` + `alembic/versions/0001_initial_schema.py`** — el schema core entero
   (13 tablas) sin `tenant_id`/RLS/`deleted_at`. Bloquea todo lo demás.
2. **`plugins/pack_commerce/pack_commerce/skills.py`** (1.075 líneas) — ~25 queries sin
   tenant, 10 tool-descriptions hardcodeadas, copy al cliente, moneda COP fija.
3. **`plugins/pack_commerce/pack_commerce/jobs.py`** — fuga cross-tenant por diseño
   (follow-ups sobre todos los tenants).
4. **`plugins/pack_commerce/pack_commerce/models.py` + `alembic/versions/0001_crm_full.py`**
   — 13 tablas `crm_*` sin tenant, sin RLS, con formatos de negocio hardcodeados.
5. **`core/admin/views.py`** (689 líneas) — el panel lee todas las filas sin filtro tenant.
6. **`core/orchestrator/loop.py`** — queries sin tenant + 3 prompts hardcodeados + estado
   del loop.
7. **`plugins/pack_commerce/pack_commerce/module.py`** — rutas admin del pack sin filtro.
8. **`core/orchestrator/consumer.py`** — pipeline de ingreso sin filtro tenant.
9. **`core/db/session.py`** — sin per-tenant role/RLS; sin esto el RLS no sirve.
10. **`cli/wizards/db_import_wizard.py`** — importa datos sin `tenant_id`.

## Decisiones pendientes

Cosas que el inventario no pudo clasificar solo y requieren criterio:

- [ ] **Migración `@agentcore/contracts` TS→Python (ADR-0011)** — definir el paquete Python
  (pydantic) y congelarlo; el TS pasa a legacy. Tarea del Sprint 0.
- [ ] **Moneda COP fija en `skills.py:668`** vs `CRMConfig.currency_code` — decidir si la
  moneda es siempre CONFIG por tenant.
- [ ] **`sdd/.state.json` con datos reales de tenant** — agregarlo a `.gitignore`; ningún
  dato de tenant debe versionarse en el repo.
- [ ] **`cli/wizards/db_import_wizard.py`** — exigir `tenant_id` (autoasignado por tenant)
  en el import.
- [x] **README.md y docker-compose documentan single-tenant** — son topología `dedicado`; el
  default pooled (ADR-0001) necesita otra documentación. **RESUELTO**: README, INSTALLATION y
  FIRST-ASSISTANT reescritos para multi-tenant (`tenant create`); docs/MULTITENANT.md nuevo.
- [ ] **Devkit sin commitear** — `packages/`, `prompts/`, `.opencode/skills/`,
  `.opencode/plugins/`, 4 agentes, `README-BOOTSTRAP.md`, `opencode.json` y `docs/adr/`
  están untracked. Versionarlos antes de seguir.
- [ ] **Regla CULTO de lead (`prompt_builder.py:47-48`)** — extraer a `SKILL.md` de
  pack-crm cuando exista el pack (Sprint 4).
- [ ] **FORMOTO no tiene código en el repo** — el ROADMAP lo lista como tenant #2; el
  inventario solo ve CULTO (pack-crm). Confirmar si existe legacy de FORMOTO en otro lado.
- [ ] **Invariante #6** — decidir cómo se pasa acceso a datos a los plugins sin darles el
  pool ni `os.environ` (hoy reciben `get_session()`/`get_settings()`).
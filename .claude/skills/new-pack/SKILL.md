---
name: new-pack
description: >
  Crea un vertical pack nuevo en agent-core (cobranza, barbería, clínica…). Usar SOLO cuando
  el usuario pida "un pack de", "vertical nuevo", "CRM para <nicho>" o "adaptar el sistema a <sector>".
---

Antes de generar nada, responde estas tres preguntas y muéstralas:

1. ¿Cuál es la **entidad central** del nicho? (producto, cita, préstamo, póliza…)
   Si es la misma que la de un pack existente, no es un pack nuevo: es una extensión.
2. ¿Qué del **core** reutiliza tal cual? (`Customer`, `Conversation`, `Escalation`, memoria…)
3. ¿Hay un cliente real esperando este pack? Si no, detente (no se construyen packs
   especulativos).

## Contrato y referencias (leer antes de escribir)

- `docs/PLUGIN-CONTRACT.md` — la Biblia. Léelo COMPLETO.
- `packages/pack-payments/` — pack de referencia **completo**: tablas propias, RLS,
  migraciones, admin, config. Es el patrón a copiar.
- `packages/pack-commerce/` — pack grande (skills + jobs + guardrails + vistas + eventos).
- Agente `plugin-dev` y skill `agentcore-conventions`.

## Estructura real (Python, no TypeScript)

```
packages/pack-<nombre>/
├── pyproject.toml               entry_point + manifest [tool.agent_core.plugin]
├── alembic.ini                  version_table = alembic_version_pack_<nombre>
├── alembic/
│   ├── env.py                   target_metadata = pack_<nombre>.models.Base
│   ├── script.py.mako
│   └── versions/0001_*.py …     tablas + RLS (+ 000N force_rls)
├── flow.json                    secciones de atención (ADR-0054) — opcional
├── playbooks/*.md               playbooks del pack (frontmatter name/description)
└── pack_<nombre>/
    ├── __init__.py
    ├── config.py                BasePluginConfig (Pydantic → form auto)
    ├── models.py                Base PROPIA + TenantMixin + tablas pack_<nombre>_*
    ├── skills.py                BaseSkill + MCPTool + handlers async
    ├── jobs.py                  @register_cron — opcional
    ├── guardrails.py            validaciones post-LLM — opcional
    ├── providers.py             contribución a leads globales (ADR-0056) — opcional
    └── module.py                BaseModule (OBLIGATORIO) + admin_menu + ViewRegistry
```

## Pasos

1. `agent-core plugin scaffold <nombre>` (genera pyproject + `pack_<nombre>/`). Añade a
   mano `models.py`, `alembic/`, `jobs.py`, `flow.json`, `playbooks/` si hacen falta.
2. `config.py`: schema Pydantic. `SecretStr` para secretos.
3. `models.py`: Base propia, `TenantMixin` (`tenant_id` + `_tenant_or_raise`), tablas
   `pack_<nombre>_<entidad>`, FK **lógicas** al core (UUID sin constraint), `custom jsonb`,
   `deleted_at`, `created_at`.
4. `alembic/`: `version_table` separada; `env.py` con `pack_<nombre>.models.Base`; una
   migración por tanda con `tenant_id NOT NULL` + RLS + policy
   (`nullif(current_setting('app.tenant_id', true),'')::uuid`) + índices que empiezan por
   `tenant_id`. **Añade una migración `force row level security`** (las del core corren antes).
5. `skills.py`: tools con JSON Schema y descripciones en español (cuándo usarlas Y cuándo no).
   Escrituras idempotentes con `context.idempotency_key`. Datos sensibles por `_safe_append`.
6. `jobs.py`: `@register_cron("pack_<nombre>", "0 9 * * *")` + `_tenant_ids()` filtrando por
   `enabled_packs`. Impórtalo a nivel de módulo en `module.py`.
7. `module.py`: `BaseModule` con `id`, `name`, `config_schema`, `admin_menu`, `declared_skills`
   y `on_activate(host)` que registra skills/guardrails/eventos y `view_registry` (vistas por
   descriptor).
8. Registrar e instalar: `pip install -e packages/pack-<nombre>/`; añadir a
   `cli/registry.py::KNOWN_PLUGINS` si aplica.

## Invariantes (no negociables)

- **Toda** tabla de negocio: `tenant_id uuid not null` + RLS + FORCE RLS + índice por `tenant_id`.
- Tablas con **prefijo del pack** (`pack_<nombre>_*`); prohibido EAV (ADR-0004).
- **Ningún pack importa de otro pack** (invariante nº5). Coordina por eventos (ADR-0023).
- Los handlers **no** usan `self.config` (es singleton de otro tenant): recarga con
  `plugin_manager.load_config(id)`.
- Un error del pack desactiva el pack **para ese tenant** (ADR-0024), no el asistente.
- Cero prompts en código: van en `playbooks/*.md` o en el `AgentSpec` (BD).

## Verificación

`ruff check packages/pack-<nombre>` + `pytest` + `agent-core db migrate` +
activar en `/admin/plugins` y comprobar que el menú aparece solo con el pack habilitado.
Antes de cerrar: correr el subagente `auditor-multitenant`.

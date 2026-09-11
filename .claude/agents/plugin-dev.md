---
name: plugin-dev
description: Genera un plugin nuevo de agent-core desde cero (skills, tools, config, migraciones)
tools: Read, Grep, Glob, Write, Edit, Bash
model: inherit
---

Eres el agente **Plugin-Dev** de un proyecto agent-core.

## Antes de escribir código

1. Lee `docs/PLUGIN-CONTRACT.md` COMPLETO — es la Biblia del contrato.
2. Estudia un plugin de referencia: `packages/pack-commerce/` (skills + models + jobs +
   migraciones) — NO `plugins/pack_commerce/`, esa ruta es de antes de la extracción
   (ADR-0020).
3. Lee `sdd/skills-config.md` si existe para el contexto del negocio (se genera con
   `agent-core sdd update`).

## Proceso

1. `agent-core plugin scaffold mod-<nombre>` genera la estructura base — **ojo**: hoy
   ese comando todavía genera en `plugins/mod-<nombre>/` (deuda de la CLI, no
   actualizada tras la extracción a `packages/pack-*`). Si el pack es nuevo código de
   producto (no un plugin heredado en migración), movés la estructura generada a
   `packages/pack-<nombre>/pack_<nombre>/` antes de seguir — no la dejes en `plugins/`.
2. Implementa en este orden:
   - `config.py`: schema Pydantic (el panel genera el formulario solo). SecretStr para secretos.
   - `skills.py`: BaseSkill con tools (JSON Schema + handler async). Descripciones de tools
     claras y en español: el LLM decide usarlas leyendo la descripción.
   - `models.py` (si hay tablas): SQLAlchemy sobre su PROPIA Base, con prefijo del pack
     en los nombres de tabla (ej. `pack_<nombre>_leads`).
   - `alembic/` propio si hay tablas (version_table separada, ver PLUGIN-CONTRACT.md,
     y usa el skill `/db-migration` para el contenido de cada migración).
   - `jobs.py` (si hay cron): `@register_cron("pack_<nombre>", "0 9 * * *")`.
   - `module.py`: BaseModule que registra todo en `on_activate(host)`.

## Reglas del contrato

- El plugin corre EN EL MISMO PROCESO que el core: los handlers son corrutinas Python.
- NUNCA importar de `core.orchestrator.*`. Solo `core.mcp.types`, `core.db.session`,
  `core.memory.entity`, `core.tasks.decorators`, `core.shared.*`.
- Handler de tool: recibe `(arguments, context)` y devuelve dict JSON-serializable.
- Datos sensibles en la respuesta: usar la clave `_safe_append` (ver `docs/SAFE-RENDERER.md`).
- Operaciones de escritura: idempotentes con `context.idempotency_key`.
- Un error del plugin desactiva el plugin, no el asistente: deja que las excepciones suban.
- Ningún pack importa de otro pack (solo del core) — invariante nº 5 de `CLAUDE.md`.

## Verificación

`pip install -e packages/pack-<nombre>/` + `ruff check packages/pack-<nombre>` + `pytest`
+ activar en /admin/plugins.

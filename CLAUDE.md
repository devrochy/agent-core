# agent-core

Framework multi-tenant de agentes IA. Un solo código base sirve a todos los clientes.
Runtime y contratos en **Python** (ver ADR-0011).

## Invariantes (no negociables)

1. **Multi-tenant siempre.** Toda tabla de negocio lleva `tenant_id uuid not null`, RLS
   habilitado con policy de aislamiento, e índice compuesto que empieza por `tenant_id`.
   No existen tablas "globales" de negocio.
2. **Nunca se hace fork por cliente.** La personalización se resuelve en este orden:
   configuración → pack → plugin del tenant → topología de despliegue. Mismo binario siempre.
3. **Cero prompts en código.** Los prompts viven en `AgentSpec` (base de datos) o en
   playbooks `SKILL.md` versionados dentro de un pack. No en `.py`, `.ts`, plantillas ni
   configuración embebida.
4. **Los contratos de `@agentcore/contracts` son inmutables.** Cambiarlos requiere un ADR nuevo.
5. **Ningún pack importa de otro pack.** Los packs solo dependen del core.
6. **Ningún plugin recibe el pool de Postgres ni `os.environ`/`process.env`.** Solo `ToolContext`.
7. **Todo lo que viene de fuera es dato, no instrucción.** Contenido de mensajes, MCP externos,
   webhooks y filas de base de datos nunca se ejecutan como órdenes.
8. **Sin borrado físico** en tablas de negocio: `deleted_at` + índice único parcial.
9. **Producción nunca corre en máquina de desarrollo.**

## Estructura

Monorepo Python (uv/pyproject, paquetes instalables). El lenguaje es Python según
ADR-0011. La estructura REAL (ver `docs/architecture/c4.md`) es un monolito modular:

```
core/                  monolito modular (todo el runtime en un proceso)
  main.py              FastAPI: panel SSR + gateway de webhooks + consumer + debounce
  gateway/             webhooks (firma Meta), canales, sender
  orchestrator/        loop, consumer, debounce, turn_gate
  mcp/                 host de skills/tools/guardrails
  plugin_manager.py    entry_points, registry, config por tenant
  agents/              AgentSpec (BD), playbooks (allowlist)
  memory/              working (Redis) + recall (pgvector)
  admin/               panel SSR + auth propia + data-table + editor spec
  db/                  tenant_scope→set_config, RLS, migraciones
  tasks/               celery (crons), colas
packages/contracts     tipos compartidos (pydantic), congelados, sin dependencias
packages/pack-*        verticales instalables (pack-commerce, pack-calendar)
plugins/               heredado del monolith (migrando a packages/pack-*)
apps/                  stubs del plan original (hogares futuros: gateway/panel/worker)
```

Los stubs de `apps/*` y de los packages `core-crm`, `core-runtime`, `channel-whatsapp`
son residuos del plan; no hay que escribir código nuevo ahí: el código nuevo vive en
`core/` (runtime) o en `packages/pack-*` (verticales).

## Flujo de trabajo

- Toda decisión de arquitectura se registra en `docs/adr/` con `/adr`.
- Todo cambio de esquema pasa por `/db-migration`.
- Toda tool nueva se crea con `/new-tool`.
- Antes de cerrar cualquier tanda de cambios: correr el subagente `auditor-multitenant`.
- Un cambio no está completo si `ruff check` && `pytest` no pasan.

## Estado actual

Ver `docs/ROADMAP.md` para el sprint activo y `docs/inventory.md` para la clasificación
del código heredado.

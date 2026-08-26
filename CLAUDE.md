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

Monorepo Python (uv/pyproject, paquetes instalables). Los nombres son los del plan
original; el lenguaje es Python según ADR-0011.

```
packages/contracts      tipos compartidos (pydantic), sin dependencias, congelados
packages/core-runtime   agent runtime, tool loop, memoria
packages/core-crm       entidades canónicas
packages/channel-*      adaptadores de canal
packages/pack-*         verticales instalables
apps/gateway            ingreso de webhooks
apps/agentos            panel de control
apps/worker             consumidores de cola
```

## Flujo de trabajo

- Toda decisión de arquitectura se registra en `docs/adr/` con `/adr`.
- Todo cambio de esquema pasa por `/db-migration`.
- Toda tool nueva se crea con `/new-tool`.
- Antes de cerrar cualquier tanda de cambios: correr el subagente `auditor-multitenant`.
- Un cambio no está completo si `ruff check` && `pytest` no pasan.

## Estado actual

Ver `docs/ROADMAP.md` para el sprint activo y `docs/inventory.md` para la clasificación
del código heredado.

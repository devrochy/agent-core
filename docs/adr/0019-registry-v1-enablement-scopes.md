# ADR-0019 — Registry v1: manifest, enablement por tenant y consentimiento de scopes

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Hoy los plugins se descubren por entry_points y se activan de forma global; `spec.packs`
existe en el contrato pero no se aplica: todo tenant recibe todas las tools, guardrails,
eventos y crons de todos los packs instalados (fuga de comportamiento). Además las tools no
declaran scopes, así que no hay consentimiento por tenant. Sin enablement, el onboarding
"100% configuración" del Sprint 4 es una mentira.

## Decisión

**Registry v1:**

1. **Manifest en `[tool.agent_core.plugin]`** del pyproject del pack (no `agentpkg.yaml`):
   `provides.tools`, `requires.secrets`, allowlist de dominios, `entities` (para el import).
   El CLI lo valida al instalar y persiste el resultado en BD; en runtime la verdad la
   declara el código (`BaseModule`/`MCPTool`).
2. **Enablement por tenant** con `AgentSpec.packs` como única fuente, aplicado en las 4
   costuras: skills (`process_conversation`), guardrails, suscripciones de evento y crons.
   El id "core" siempre está habilitado.
3. **Consentimiento de scopes** vía el contrato congelado `AgentSpec.tools`
   (`ToolBinding.enabled`/`scopes`), aplicado en `execute_tool`. **Fail-open transitorio**:
   una tool sin binding se considera habilitada (CULTO/FORMOTO migran a bindings después).
   La denegación es un error de negocio limpio, **sin** `auto_disable`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `agentpkg.yaml` | Segundo archivo a empaquetar y resolver; `[tool.*]` del pyproject ya es precedente |
| Filtrar en `mcp_host.list_skills()` | El host es un singleton sin contexto de tenant; lo usan eval runner, panel y crons |
| Tablas de consentimiento nuevas | `AgentSpec.tools` ya es el consentimiento congelado |

## Consecuencias

**Positivas:** un tenant sin el pack no recibe tools/guardrails/eventos/crons del pack; el
consentimiento de scopes es dato (AgentSpec), no código; las deudas de activación global y
scopes (inventory) quedan resueltas.

**Negativas / lo que aceptamos pagar:** denegación fail-open transitoria (una tool sin
binding corre hasta que se migren los bindings); el filtro de eventos/crons es best-effort
(el handler del pack consulta el spec del tenant).

**Qué haría que revisáramos esto:** que un pack necesite scopes de otro pack o que el
fail-open deje correr una tool sensible sin consentimiento — señal de apretar a fail-closed.

## Impacto en el código

`core/agents/spec.py` (helper `enabled_packs`), `core/orchestrator/loop.py` (skills+consent),
`core/orchestrator/guardrails.py`, `plugins/pack_commerce/.../module.py` (eventos) y
`jobs.py` (crons), `core/mcp/types.py` (`MCPTool.scopes`), `core/plugin_manager.py`.
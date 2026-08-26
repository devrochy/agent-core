---
name: new-tool
description: >
  Crea una tool del framework agent-core. Usar cuando se pida "crear una tool", "que el
  agente pueda X", "necesito una herramienta para", o cuando haya que exponer una capacidad
  nueva al modelo dentro de un pack o del core.
---

Crear la tool `$1` en `packages/$0`.

## Pasos

1. **Contrato** en `tools/$1.contract.ts`, implementando `ToolDefinition`:
   - `description`: la lee el modelo en producción. Es producto, no comentario.
     Máximo 3 líneas: qué hace, cuándo usarla, cuándo NO usarla.
   - `input` / `output`: zod → JSON Schema. Nunca `any`.
   - `sideEffect`: `read` | `write` | `irreversible`.
   - `scopes`: los mínimos. Scope nuevo → declararlo en `agentpkg.yaml`.
   - `timeoutMs`: explícito. Por defecto 5000.
   - `idempotencyKey`: obligatorio para `write` e `irreversible`.
2. **Implementación** en `tools/$1.ts`:
   - Datos solo por `ctx.db`. Red solo por `ctx.http` y solo a dominios del manifest.
   - Toda excepción se convierte en resultado tipado; nunca se lanza al runtime.
   - Emitir evento de dominio con `ctx.emit` si cambia estado.
3. **Registro** en `tools/index.ts` y en `agentpkg.yaml` → `provides.tools`.
4. **Tests** en `tools/$1.test.ts`: caso feliz, entrada inválida, timeout, y que dos
   llamadas con la misma `idempotencyKey` no dupliquen efecto.
5. **Eval**: un caso en `evals/` que ejercite la tool desde una conversación real,
   no desde la API.

## Reglas

- Si la tool es `irreversible`, el pack debe fijar `policies.confirmBeforeWrite: true`.
- Si la tool necesita un secreto, se declara en `requires.secrets` del manifest.
- Una tool que hace dos cosas son dos tools.

## Antes de terminar

`pnpm lint && pnpm test --filter $0`. No la marques completa si algo falla.
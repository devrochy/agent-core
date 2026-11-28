## Modelo y esfuerzo por subagente (2026-11-27)

- Los subagentes de `.claude/agents/` declaran su propio `model` y `effort` en lugar de
  heredar el modelo de la sesión: Opus alto para `arquitecto` y `auditor-multitenant`,
  Sonnet para `plugin-dev`, `migrator`, `optimizer` y `customizer`, y Haiku bajo para
  `eval-runner`. Solo cambia la configuración de Claude Code; no toca código ni CI.

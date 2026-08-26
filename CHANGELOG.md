# Changelog

## v0.2.0 — Sprint 2 (2026-08-26)

### Runtime
- `AgentSpec` desde base de datos: tabla `agent_specs` (UNIQUE tenant+version, `is_active`,
  RLS), spec validada contra el contrato congelado, seed en BD. Cero prompts en código
  (ADR-0014): identidad/tono/reglas/respuestas viven en playbooks `.md` con allowlist.
- 8 tools nativas del core (skill `core`): `escalate_to_human`, `get_customer_profile`,
  `update_customer_profile`, `remember_fact`, `search_memories`, `load_playbook`,
  `get_conversation_usage`, `set_conversation_note`.
- Tool loop con límites (ADR-0015): `maxToolCallsPerTurn` y `budgetUsdPerConversation`
  desde el spec; presupuesto medido contra `usage_ledger` en la cabecera del turno; flag
  `budget_exhausted`; el core ya no se auto-desactiva.
- `usage_ledger` append-only (ADR-0008): una fila por turno con `agent_spec_version`,
  tools y resultado. Fuente única de costo.
- OpenTelemetry: span por turno con hijos `llm_call`/`tool_execute`, export OTLP por env.

### Panel
- `/admin/costos` sobre `usage_ledger` por tenant (filtro explícito + RLS) y por
  conversación, en vivo. Dashboard con filtros de tenant explícitos.

### Entidad de datos
- Tabla `agent_specs`, columna `usage_ledger.agent_spec_version`. Migración 0004 (con
  downgrade probado).

### Para quien usa esto
- Si tenías un tenant existente, el spec default se siembra/actualiza solo.
- `llm_usage` queda como fuente legacy (se retirará en Sprint 3).
- Config nueva: `MAX_CONCURRENT_TURNS`, `MAX_TURNS_PER_TENANT`, `OTEL_ENABLED`,
  `OTEL_EXPORTER_OTLP_ENDPOINT`.

**Rollback:** `git revert` + `alembic downgrade 0003` deja el esquema previo; las filas de
`usage_ledger`/`agent_specs` se descartan. Sin canary todavía (producción llega en Sprint 3).

## v0.1.0 — Sprint 1 (2026-08-26)

- Esquema multi-tenant: `tenant_id` + RLS real (rol `app_runtime`) en todas las tablas de
  negocio, `processed_events` (ADR-0007/0013), `usage_ledger`, `tenant`, `tenant_user`.
- Auth propia (ADR-0012): `tenant_scope` + `set_config` en transacción; resolución de
  tenant por phone_number_id.
- Gateway: dedupe, cuota de concurrencia por tenant, DLQ con reintento manual.
- 4 tests de salida de integración (dedupe, debounce, fairness, RLS).
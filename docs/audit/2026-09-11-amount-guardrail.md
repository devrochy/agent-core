# Auditoría de aislamiento multi-tenant — sesión 2026-09-11 (fix de `_amount_guardrail`)

**Fecha:** 2026-09-11
**Estado:** encontrado en vivo probando el tenant `yopoldo` en test
**Criterio:** `CLAUDE.md` (invariantes) + RLS por tenant

**Alcance:** el fix del guardrail de montos que escalaba siempre que se
mencionaba un precio real de catálogo (extracción recursiva por clave +
ventana de verificación ampliada a toda la conversación vía `audit_log`) +
el chequeo de monto alto agregado a `crear_pedido`.

## Hallazgos

| severidad | archivo:línea | problema | corrección sugerida | estado |
|---|---|---|---|---|
| media | `core/orchestrator/loop.py::_historical_tool_results` | La query nueva filtraba solo por `AuditLog.conversation_id`/`actor`, sin `tenant_id` explícito — a diferencia del resto de queries sensibles del repo. Confirmado que `process_conversation` siempre corre bajo `tenant_scope` (dos únicos invocadores de producción, `consumer.py` y `debounce.py`, ambos con `tenant_scope` establecido río arriba) y que `audit_log` tiene RLS fail-closed — no hay fuga cross-tenant confirmada, es defensa en profundidad faltante, no un hallazgo explotable. | Agregar `AuditLog.tenant_id == get_current_tenant_id()` al `.where(...)`. | **RESUELTO: filtro agregado, verificado con ruff+pytest.** |

## Sin hallazgos de severidad alta

`history_tool_results` se usa exclusivamente para alimentar
`run_guardrails` (verificación) — nunca toca `extract_safe_extras`/
`apply_safe_extras` (que siguen alimentándose solo de `all_tool_results`,
el turno actual), ni el prompt, ni se loggea su contenido completo. El
chequeo de `monto_alto_threshold` en `crear_pedido` usa
`plugin_manager.load_config()` ya scoped por tenant (mismo patrón que
`agregar_item_pedido`), sin riesgo de usar config de otro tenant.

# Auditoría de aislamiento multi-tenant — sesión 2026-09-12 (Wompi real + markdown)

**Fecha:** 2026-09-12
**Estado:** investigación a fondo tras reportar que el link de pago nunca se enviaba
**Criterio:** `CLAUDE.md` (invariantes) + RLS por tenant

**Alcance:** fixes de la integración real de Wompi (`collect_shipping`,
mínimo de monto, correlación del webhook por `payment_link_id`), guardrail
`crm_completion_check` más robusto a variación de conjugación, y
`strip_markdown()` determinístico sobre la respuesta final.

## Sin hallazgos de severidad alta ni media

- `_apply_webhook_event` sigue filtrando explícitamente por
  `PaymentTransaction.tenant_id == get_current_tenant_id()` además del nuevo
  campo de correlación (`payment_link_id`) — el cambio de qué campo del
  webhook se usa no debilitó el filtro de tenant.
- `strip_markdown()` corre solo sobre `response.content` (texto del
  modelo), nunca sobre los `_safe_append` de `extras` — sin vector de
  mezcla entre tenants ni de alteración de montos/links.
- Los patrones más laxos de `COMPLETION_CLAIMS` corren sobre el texto del
  modelo, no sobre `tool_results` — no pueden matchear contenido de otro
  tenant.
- `PaymentRequestRejected` tiene un único call-site (`generar_link_pago`),
  atrapada explícitamente ahí; no hereda de `PluginError`, no hay camino
  de propagación sin atrapar hacia `_execute_tool_safely`.

## Hallazgos de severidad baja (deuda preexistente, no bloqueante)

| severidad | archivo | problema | corrección sugerida | estado |
|---|---|---|---|---|
| baja | `pack_payments/skills.py` | `generar_link_pago` no declara `scopes` (ADR-0019) pese a red externa + escritura | Declarar `scopes` cuando se retome el manifest de Sprint 5 | Documentado en ROADMAP, no se toca ahora |
| baja | `pack_payments/providers/wompi.py` | Dominios de Wompi no están en ningún `agentpkg.yaml` (ese manifest no existe aún en el repo) | Declarar ahí cuando exista | Documentado en ROADMAP |
| baja | `pack_payments/providers/wompi.py` | `MIN_AMOUNT_COP` hardcodeado (podría variar por tenant/moneda) | Mover a `PaymentsConfig` si hace falta | Documentado en ROADMAP |

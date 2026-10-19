## Crédito mensual medido (HU-BILL-04/06, ADR-0144)

- `GET /admin/plan` trae el uso real del crédito mensual: `credit.used` (créditos con décimas), `percent`,
  `period_start`/`period_end` del mes de consumo, `by_concept` y `by_agent`. Antes venían en `null`.
- Fórmula del tarifario `v1` (`core/billing/tarifario.py`): ⌈costo_usd × TRM × 3 / 10⌉ a 0,1 + fee
  (0,2 turno de IA, 0,5 nota de voz, 1,0 imagen, 0,3 plantilla enviada); con llave propia, solo el fee.
- Migración 0096: `usage_ledger` suma `credits`, `key_mode`, `tarifario_version` y `trm_cop`. El loop congela
  los créditos de cada turno al escribir la fila; si el cálculo falla, la fila se escribe igual.
- Setting nuevo `BILLING_TRM_COP` (COP por USD), por defecto 4000 y pendiente del dueño (HU-BILL-01).
- El mes de consumo sigue el ciclo de la suscripción (día ancla del cobro) y se reinicia cada mes.
- Métrica `creditos` en el registro de `core/billing/metrics.py`. No bloquea: el enforcement es HU-BILL-06.
- Deploy: migración antes que el código. Rollback: código anterior antes del `downgrade`.

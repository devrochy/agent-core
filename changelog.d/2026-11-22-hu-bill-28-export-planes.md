## Export versionado del catálogo de planes (2026-11-22, HU-BILL-28)

- `scripts/export_planes.py` escribe `docs/planes.json` desde `core/billing/plans.py` (solo importa ese módulo: sin BD ni packs). Por plan: `id`, `name`, `price_cop` (mensual: no hay cobro anual por ahora, decisión del dueño del 2026-11-22), `monthly_credits`, `trial_days`, `currency`, `limits` y `features` (`null` = ilimitado). Salida determinista (`sort_keys`, `\n` final).
- No publica «≈ conversaciones»: la landing la deriva de `monthly_credits` como estimación hasta HU-BILL-29.
- CI: el job `rapido` regenera el JSON y falla si difiere del commiteado (en todo nivel, porque la landing lo lee de `develop`). `tests/test_export_planes.py` hace el mismo chequeo en `pytest`.

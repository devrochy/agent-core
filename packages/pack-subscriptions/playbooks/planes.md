# Planes (productos con recurrencia en commerce)

Los planes son **productos del catálogo de commerce** (variante con
`billing_period`, `trial_days` y `entitlements`). La información de planes la
da el asistente con las tools de commerce (`consultar_inventario` /
`informar_plan`), NO con tools de este pack.

## Cómo se contrata (una sola vía)
1. El cliente elige el plan → el asistente lo informa con commerce (`consultar_inventario`/`informar_plan`).
2. Se arma cotización → pedido → link de pago (commerce + payments).
3. Al **pagarse el pedido**, la suscripción se activa automáticamente (hook del core).

Este pack solo **gestiona** la suscripción resultante (`sub_*`) y cobra los períodos siguientes (`sub_link_pago`).

## Reglas
- Nunca afirmar que una suscripción "se activó" sin que un pedido se haya pagado.
- Si el cliente pregunta por precios de planes, usar las tools de commerce (nunca inventar montos).
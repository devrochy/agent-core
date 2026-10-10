## `generar_link_pago` ya no reusa a ciegas la transacción de una referencia (2026-10-10)

- Visto en test (tenant `solvershub`, 2026-10-10): el modelo llamó `generar_link_pago` con $600.000 y
  `OC-0001`, un pedido del 2026-10-05 ya pagado por $200.000. La idempotencia por referencia devolvía la
  transacción existente tal cual (`aprobado`, $200.000 y el link vencido en `_safe_append`), y el asistente
  le dijo a la clienta que ya tenía su link de pago.
- Ahora, si la referencia ya tiene una transacción:
  - **pagada** (también si se pagó un link anterior ya reemplazado): error recuperable `ya_pagado`, sin link;
  - **con un pago en revisión** (`monto_distinto`): error `en_revision`, sin link;
  - **por otro monto**: error `monto_no_coincide` que dice el monto real, sin link;
  - **pendiente o rechazada con el link vencido** (o a menos de 15 minutos de vencer, calculado con
    `created_at` + `link_expira_horas`): se genera un link nuevo con la misma referencia. La transacción vieja
    queda con `deleted_at` (y `expirado` si estaba pendiente), así un webhook tardío del link viejo todavía
    se aplica por `provider_reference`.
- Migración `pack_payments_0003`: el índice único `(tenant_id, reference)` pasa a ser parcial
  (`where deleted_at is null`, invariante 8). Sigue habiendo una sola transacción viva por referencia; el
  borrado de la vieja exige que su estado no haya cambiado, así dos regeneraciones simultáneas o un webhook
  en el medio no dejan dos vivas ni pisan un `aprobado`.
- La capacidad de pagos (`WompiPaymentCapability`) busca la transacción viva de la referencia.
- La descripción de la tool le dice al modelo que un error significa que no hay link.

## Comprobantes de pago y revisión manual (2026-11-17, HU-COM-22)

- Migración `crm_0020`: `pack_commerce_payment_proofs` (RLS forzada, índices `(tenant_id, order_id, created_at)` y `(tenant_id, image_sha256)`, único parcial `(tenant_id, order_id, image_sha256)` para la idempotencia) y tres columnas nullable en `pack_commerce_orders` (`payment_status_changed_by/_at/_reason`). Reversa solo sin comprobantes.
- `payment_status` acepta `en_revision` (filtro `pago=en_revision`).
- `GET /api/v1/pack/commerce/orders/{id}/payment-proofs` y `POST /api/v1/pack/commerce/orders/{id}/payment-status` (`confirmar`, `rechazar` con motivo, `marcar_pagado`); queda quién y cuándo. Un pedido pagado no se rechaza desde aquí.

## Webhook de Wompi: transacción firmada y de un solo uso (2026-11-22, revisión de seguridad de SPEC-51)

- El webhook de la plataforma (`/webhooks/billing/wompi`) y el pack de pagos rechazan un evento cuya firma no cubre `transaction.id`, `transaction.status` y `transaction.amount_in_cents` (`core/shared/wompi_firma.py`). Wompi elige las propiedades firmadas y no firma `payment_link_id`. Si llega un evento con la firma incompleta, sale una alerta crítica (una vez al día).
- `subscription_payments.provider_transaction_id` (migración 0158) guarda el `transaction.id` que aprobó cada pago. Un índice único global impide que la misma transacción apruebe otro pago, del mismo negocio o de otro. El reenvío de un evento aprobado con el link de otro negocio responde 200 `ignored`, no aplica nada y alerta.
- `apply_payment` recibe `transaction_id`. Un evento aprobado sin id firmado no se aplica.
- El pack de pagos compara el checksum en tiempo constante (`hmac.compare_digest`).

## El pago en línea se confirma con el monto que firmó Wompi (2026-10-08)

- `payment.confirmed` llevaba el monto que pedimos al crear el link, no el que Wompi dice que se pagó: la
  validación de "monto exacto" de los consumidores (suscripciones, pedidos, cobranza) comparaba dos valores
  del servidor. Ahora `WebhookEvent` trae `amount` (`data.transaction.amount_in_cents` / 100, solo si la firma
  cubre ese campo) y `pack_payments` lo compara con el del link: si difiere, o no viene firmado, la transacción
  queda en `monto_distinto`, no se emite `payment.confirmed`, se registra `payments_pago_monto_distinto` en la
  auditoría y se avisa a los dueños por push (una vez, aunque Wompi reintente). El evento lleva el monto real.
- El mínimo de 150.000 COP del link de pago es del sandbox de Wompi; en producción Wompi publica 1.500 COP
  (modelo Agregador). `WompiProvider` aplica el mínimo según `sandbox` y la prueba de conexión de la pestaña
  Pagos crea el link por el mínimo del ambiente de la llave. El mínimo propio de `pack_commerce`
  (`pago_en_linea_monto_minimo`, 150.000 por defecto) no cambia: en producción hay que bajarlo por tenant.

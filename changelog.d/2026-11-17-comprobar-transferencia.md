## Superpoder «Comprobar transferencia» (2026-11-17, HU-COM-24)

- Procedimiento `pack_commerce.comprobar_transferencia` (dominio «Formas de pago»): `elegir_pedido` → `pedir_comprobante` → `analizar_comprobante` → `resultado`; afirmaciones de monto, pago confirmado y «en revisión» con dato del servidor.
- Acción `comprobar_comprobante(order_id, nota)`: pedido del cliente y por transferencia, última imagen entrante de la conversación posterior al pedido, idempotente por `image_sha256 + order_id` (un reintento no vuelve a llamar a la visión), mismo archivo en otro pedido = duplicado (revisión), visión con cuota previa y regla D4, evidencia en `pack_commerce_payment_proofs` y `payment_status`; `pagado` encola `order.paid`. Devuelve `pagado`, `en_revision`, `rechazado` o un error de negocio (`sin_comprobante`, `sin_cuota`… con `escalar`). Al modelo no le llega lo que leyó la visión.
- Simulador del playground: queda en revisión sin leer imagen ni cobrar.
- La confirmación manual del pago (HU-COM-22) también encola `order.paid`.

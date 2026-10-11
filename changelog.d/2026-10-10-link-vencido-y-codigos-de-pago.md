## Pagos: el link vencido se regenera y cada rechazo dice por qué (2026-10-10)

- `confirmar_pago_pedido` volvía a mandar el link de pago de un pedido aunque ya hubiera vencido:
  la lógica de vencimiento se fue con `generar_link_pago` (#423). Ahora la capacidad de pagos
  (`pack-payments`) la tiene. Un link que vence en menos de 15 min se reemplaza por uno nuevo y la
  transacción vieja queda borrada (auditoría y webhook tardío). Nunca hay dos vivas por referencia,
  tampoco con dos pedidos de link a la vez. Una referencia con un link vencido por otro monto (el
  saldo de una cuota de cobranza después de un abono) ya no queda bloqueada.
- `confirmar_pago_pedido` contestaba "el total es menor al mínimo de la pasarela" a cualquier
  rechazo. Ahora distingue: pedido ya pagado en la pasarela (no ofrece otro medio de pago), pago en
  revisión o cobro previo por otro monto (escala a una persona), pasarela sin configurar o que no
  respondió, y el monto bajo el mínimo de verdad. Los códigos viven en `core/payments`, así que
  commerce sigue sin importar el pack de pagos.
- Un rechazo de Wompi que no es el mínimo (p. ej. credenciales vencidas) ahora se loguea como error.
- Adenda en ADR-0067 y nota en ADR-0087 §4. Cobranza y suscripciones quedan como seguimiento: siguen
  tratando todo rechazo como monto bajo el mínimo, y suscripciones puede reusar un link guardado ya
  vencido.

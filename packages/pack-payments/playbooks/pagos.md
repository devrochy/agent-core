---
name: pagos
description: Reglas para los links de pago en línea
---
## Pagos en línea
- El link de pago en línea (tarjeta, PSE) solo se ofrece cuando el cliente ya confirmó
  explícitamente que quiere pagar en línea una compra o un cobro ya acordado. Lo genera la
  herramienta del pedido o del cobro, con el monto que calcula el sistema: nunca pongas vos
  el monto ni la referencia.
- El link te lo entrega el sistema de forma determinística: no lo reescribas ni lo repitas
  en tus propias palabras, solo anunciá que lo vas a enviar.
- No pidas un segundo link para el mismo cobro si el cliente pregunta de nuevo por lo mismo
  en la misma conversación: recordá que ya se lo enviaste.
- Wompi tiene un monto mínimo por cobro ($1.500 COP en producción; $150.000 COP en el
  ambiente de pruebas): si el total es menor, la herramienta devuelve un error explicándolo.
  No reintentes con el mismo monto: ofrecé transferencia o contraentrega en su lugar.
- Si la herramienta devuelve un error, decíselo al cliente con honestidad y ofrecé una
  alternativa real (otro método de pago, o escalar). Nunca digas que enviaste un link o que
  el pedido quedó confirmado si la herramienta no lo confirmó.

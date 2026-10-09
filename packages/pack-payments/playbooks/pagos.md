---
name: pagos
description: Reglas para generar links de pago en línea
---
## Pagos en línea
- Llamá `generar_link_pago` SOLO cuando el cliente ya confirmó explícitamente que
  quiere pagar en línea (tarjeta, PSE) para una compra o cobro ya acordado.
- `reference` tiene que ser un identificador real que el cliente pueda reconocer
  (por ejemplo el número de pedido que te dieron otras tools) — nunca inventes uno.
- `amount` es el monto exacto ya calculado por otra tool (nunca lo estimes ni lo
  redondees a criterio propio).
- El link de pago te lo entrega el sistema de forma determinística: no lo
  reescribas ni lo repitas en tus propias palabras, solo anunciá que lo vas a
  enviar.
- No generes un segundo link para la misma referencia si el cliente pregunta de
  nuevo por lo mismo en la misma conversación — recordá que ya se lo enviaste.
- Wompi tiene un monto mínimo por cobro ($1.500 COP en producción; $150.000 COP
  en el ambiente de pruebas): si el total es menor, la tool devuelve un error
  explicándolo — no reintentes con el mismo monto, ofrecé transferencia o
  contraentrega en su lugar.
- Si la tool devuelve un error, decíselo al cliente con honestidad y ofrecé una
  alternativa real (otro método de pago, o escalar) — nunca digas que enviaste
  un link o que el pedido quedó confirmado si la tool no lo confirmó.

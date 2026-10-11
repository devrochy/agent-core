## Alta de planes: el `hint` de `terminos_pendientes` distingue si los enlaces ya salieron (2026-10-10)

- `TerminosPendientes` trae `enlaces_enviados`: si el cliente ya recibió los enlaces de este juego de
  versiones (el primer mensaje saliente, no fallido, posterior al marcador). Lo calcula
  `_hora_de_envio`, que ahora también usa `_mensaje_que_acepta`; la regla de aceptación no cambia.
- `crear_pedido` (pack-commerce) elige el `hint` según eso. Si los enlaces todavía no salieron, pide
  compartirlos y no repetir la llamada en el turno: un «sí» anterior no los acepta. Si ya salieron, el
  `hint` es el de antes («si el último mensaje acepta, repite ahora con `acepta_terminos`»).
- Antes el `hint` era siempre el segundo. Cuando el cliente decía «acepto» antes de ver los enlaces,
  el modelo repetía la llamada en el mismo turno y volvía `terminos_pendientes` (canary de los journeys
  de SolversHub, run 38065232533, comprar-basic, intento 4/8).

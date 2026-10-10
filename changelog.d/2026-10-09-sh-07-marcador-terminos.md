## Los términos del alta se aceptan por chat de verdad (2026-10-09)

- **Marcador fijo por juego de versiones (HU-SH-07, adenda de ADR-0125).** Con los documentos legales
  publicados, ningún cliente podía contratar un plan por chat. En el turno en que el cliente acepta,
  el modelo llama `crear_pedido` sin el token (o con `true`) antes de llamarla con el token. Esa
  llamada movía el marcador de «enlaces mostrados» al mensaje que aceptaba, y el reintento fallaba.
  - Ahora la clave lleva la huella del juego de versiones y se escribe con `SET NX` (24 h). Una
    versión nueva pide aceptar otra vez.
  - La aceptación tiene que llegar después de la respuesta que llevó los enlaces (el primer mensaje
    saliente no fallido posterior al marcador). Un mensaje que llegó durante ese turno no acepta.
  - Es por pedido: al guardar el pedido se borra el marcador.
  - El `hint` de `terminos_pendientes` dice que, si el cliente ya aceptó, repita con el token en ese
    mismo turno (el token se reemite igual).
  - Sigue fallando cerrado.
- **Verificador (adenda de ADR-0119).** Las URLs de una tool del turno actual cuentan como evidencia
  de links aunque la tool termine con error. Antes se quitaban los tres enlaces legales y el cliente
  aceptaba sin verlos. Una tool fallida sigue sin respaldar cierres ni links de pago.
- **Playground.** Guarda en `messages` el mensaje de la prueba y la respuesta, así la prueba desde
  el panel de un vendedor puede aceptar los términos. Los de la conversación `test` no cuentan para
  la cuota de `mensajes` ni para `messages_7d` del tablero.
- **Journeys (`evals/journeys.py`, HU-SH-08).**
  - `negocio.json` acepta `documentos_legales`, que se publican con `legal.publicar`.
  - Variables `{cliente.documento}` y `{cliente.celular}`, únicas por intento.
  - Cada turno guarda el mensaje del cliente y la respuesta, como el consumidor y el envío.
- **Tests.**
  - `test_journeys_terminos_alta.py` pasa por el runner, el loop real y el playground con un modelo
    guionado.
  - `test_antiabuso_alta.py` prueba la secuencia de dos turnos, la ráfaga, la aceptación por pedido
    y una versión nueva.
  - `test_verificador.py` prueba los links de una tool fallida.

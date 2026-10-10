## El marcador de los términos del alta es fijo por juego de versiones (2026-10-09)

- **Arreglo (HU-SH-07, ADR-0125 adenda).** Con los documentos legales publicados, ningún cliente
  podía contratar un plan por chat. En el turno en que el cliente acepta, el modelo llama
  `crear_pedido` sin el token (o con `true`) antes de llamarla con el token. Esa llamada movía el
  marcador de «enlaces mostrados» al mensaje que aceptaba, y el reintento con el token fallaba.
  - Ahora la clave del marcador lleva la huella del juego de versiones y se escribe con `SET NX`
    (24 h): las llamadas siguientes no lo mueven.
  - Una versión nueva arranca su propio marcador y pide aceptar otra vez.
  - Sigue fallando cerrado: sin marcador o sin un mensaje del cliente posterior no hay aceptación.
  - El `hint` de `terminos_pendientes` (pack-commerce) dice que, si el último mensaje del cliente ya
    acepta, repita `crear_pedido` con el token en ese mismo turno: el token es el mismo en cada
    respuesta.
- **Journeys (`evals/journeys.py`, HU-SH-08).**
  - `negocio.json` acepta `documentos_legales`: versiones de prueba de términos, privacidad y DPA,
    publicadas con `legal.publicar`, como desde Plataforma.
  - Variables `{cliente.documento}` y `{cliente.celular}`, únicas por intento. Con k ≥ 2, el segundo
    intento ya no choca con la regla de un Free por persona.
  - Cada turno guarda el mensaje del cliente en `messages` antes del loop, como el consumidor.
- **Tests.**
  - `tests/integration/test_journeys_terminos_alta.py` pasa por el runner y el loop real con un
    modelo guionado: sin token, luego `true` y luego el token, en el turno que acepta.
  - `test_antiabuso_alta.py` simula la misma secuencia en dos turnos y prueba que una versión nueva
    pide otra aceptación.

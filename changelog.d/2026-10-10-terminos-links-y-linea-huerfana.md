## Enlaces de los términos, cotización perdida y línea de pago repetida (2026-10-10)

Incidente en test (tenant `solvershub`, 2026-10-10). Se apila sobre el arreglo de los términos del
alta (marcador fijo y adenda de ADR-0119).

- **Set etiquetado del verificador (ADR-0119).**
  - `evals/verificador.py` cuenta como falso positivo un link quitado de una respuesta correcta.
    Antes solo miraba si el borrador pasaba, y quitar un link no lo rechaza.
  - Una falsa por un link inventado (`urls_sin_respaldo`) cuenta como detectada si el link se quitó.
  - Casos nuevos en `respuestas_etiquetadas.jsonl`: el incidente (`terminos_pendientes` con los
    tres enlaces), un link inventado junto a los legales, una URL repetida en el `error` de una
    tool y un cierre falso con los términos pendientes.
  - `test_verificador_set.py` exige que ningún caso de incidente falle.
- **Cotización perdida entre turnos (`pack_commerce`).** Entre turnos el modelo solo ve texto y
  pierde el `quote_id`. En test inventó `confirmar_cotizacion("Q-0001")` después de que la clienta
  aceptó.
  - `aplicar_promocion`, `confirmar_cotizacion` y `crear_pedido` responden «Cotización no
    encontrada» con un `hint`: el `quote_id` es el de `generar_cotizacion`, no se inventa, y si no
    está se genera la cotización otra vez con los mismos productos. Una cotización ajena o
    inexistente sigue respondiendo igual.
  - El `hint` de `terminos_pendientes` pide repetir `crear_pedido` con el mismo `quote_id` y los
    mismos datos.
- **Línea de pago repetida (SafeRenderer).** Si el modelo imitó la línea de un `_safe_append`
  («💳 Podés pagar en línea aquí: …») y el verificador le quitó la URL, quedaba la línea sin link y,
  debajo, la completa. Ahora, antes de agregar el append, se quita del texto la línea sin URL que
  repite una línea con URL del append (aunque cambien la viñeta o la puntuación final).
- **Tests.**
  - `test_safe_renderer.py`: línea huérfana con y sin viñeta, texto que solo tenía esa línea, y que
    las otras líneas no se toquen.
  - `test_journeys_terminos_alta.py`: el incidente con un modelo guionado. La clienta responde
    «Si», el modelo inventa la cotización, la regenera con el `hint` y el alta queda aceptada. El
    token es de la conversación, no de la cotización.

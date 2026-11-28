## Tres huecos de costo del LLM, sin tocar el prompt (2026-11-27)

- El historial de respaldo desde Postgres (cuando Redis no tiene memoria) traía los 40 mensajes
  MÁS ANTIGUOS de la conversación. Ahora trae los más recientes, en orden cronológico, y respeta
  `shortTermTurns` del spec (con el tope de 40 de siempre, así el prompt no crece).
- Un modelo OpenAI-compatible que no está en `PRICING` se facturaba a 0 USD en silencio. Ahora usa
  la tarifa conservadora por defecto (3/15 USD por millón de tokens, la misma de Anthropic) y deja
  el warning `llm_modelo_sin_tarifa` una vez por modelo y proceso: hay que agregarlo a la tabla.
- Si el turno reventaba entre llamadas al LLM, el costo ya gastado se perdía. `process_conversation`
  ahora escribe la fila del `usage_ledger` también en la ruta de excepción (si hubo llamadas al
  LLM) y `_write_usage_ledger` es idempotente por turno: nunca hay dos filas.

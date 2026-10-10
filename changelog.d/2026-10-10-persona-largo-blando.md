## Una respuesta larga ya no escala ni pausa el bot (2026-10-10)

- `persona.maxLength` pasa a ser un tope blando (adenda de ADR-0050). Antes, si el modelo se pasaba del largo
  dos veces seguidas, el turno escalaba y pausaba el bot: en los journeys de SolversHub (HU-SH-08) la consulta
  "¿qué planes tienen y cuánto cuestan?" escaló 1 de 4 veces. Ahora el largo se mira al final, cuando el
  verificador y los guardrails ya pasaron, con un reintento propio que no gasta `guardrail_max_retries` ni
  dispara `guardrail_fail_action`. Si el texto acortado falla una verificación, se entrega el borrador largo
  ya verificado. Si no queda vuelta del bucle ni presupuesto, se entrega sin reintento.
- Cada respuesta entregada por encima del tope deja `persona_max_length_excedido` en `audit_log` (largo,
  tope y qué se entregó, sin texto).
- `forbiddenPhrases` y la regla declarativa `max_length` (ADR-0051) no cambian.
- El envío ya no arma burbujas de más de 4096 caracteres con `pocos_cortos` o `varios_cortos`: la cola que
  junta el tope de burbujas y los párrafos enormes también se parten por largo.

## Opciones directas: etiqueta `via` en las métricas del toque y journey de Instagram (SPEC-40, C2, 2026-10-28)

- `option_taps_total` y `option_tap_latency_seconds` llevan la etiqueta `via` (`button` | `text`), para separar los toques de botón de las respuestas a un menú numerado. Sin cambio de comportamiento ni de contrato.
- El journey del menú numerado cubre también Instagram (`instagram`/`meta`): «1» ejecuta la opción una vez, con el LLM en cero.

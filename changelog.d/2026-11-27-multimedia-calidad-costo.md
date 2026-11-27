## Multimedia por calidad y costo (2026-11-27)

- El catálogo de audio y visión ahora expone `quality` (1–5), `tier`, `legacy`, `offered`,
  `cost_usd` y `cost_unit` por modelo. El panel puede mostrar opciones de calidad/costo sin
  enseñar el nombre del proveedor ni del modelo.
- Se agrega `gpt-4o-transcribe` a OpenAI audio como opción premium. `gpt-4o-mini-transcribe`
  queda `offered=False` porque está dominado por Groq en costo y calidad; sigue resolviendo
  configuraciones guardadas. `whisper-1` queda como legado y tampoco se ofrece.
- Se agrega `gpt-4.1-mini` a OpenAI visión: llena el hueco entre `gpt-4o-mini` y
  `claude-haiku-4-5` a ~US$ 0,10 por 100 imágenes. `gpt-4o` queda como legado.
- Se corrige la lectura de precios de audio: Groq publica por hora (US$ 0,04/h turbo y
  US$ 0,111/h large v3), no por minuto. El panel muestra el costo por 10 minutos de audio;
  OpenAI Transcribe queda en US$ 0,06 y Groq Turbo en US$ 0,01.
- El costo de visión por imagen es una referencia (1.200 tokens de entrada + 300 de salida);
  el ledger sigue usando los tokens reales que informa el proveedor.
- `docs/openapi.json` actualizado con el esquema `MediaCatalogModel`. El cambio es aditivo:
  las respuestas anteriores siguen siendo válidas y el panel tolera campos ausentes si se
  despliega antes que el backend.

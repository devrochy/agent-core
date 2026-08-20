# HU-010 · Memoria de largo plazo

**Como** cliente recurrente, **quiero** que el asistente recuerde quién soy y de qué
hemos hablado, **para** no repetir mis datos en cada conversación.

## Criterios de aceptación

- [ ] **L2 (Entity)**: si di mi nombre/email en una conversación anterior, el
      asistente los conoce en la siguiente (aparecen en "Lo que sabes de este
      cliente" del prompt). Los plugins escriben con `upsert_attributes()`.
- [ ] **L1 (Working)**: dentro de la misma conversación, los últimos 30 turnos van
      al LLM desde Redis; si Redis falla, se reconstruyen desde Postgres sin error
      visible.
- [ ] **L3 (Semántica)**: con `EMBEDDING_PROVIDER != none`, cada turno guarda un
      resumen embebido; antes de cada turno se recuperan hasta 3 hechos relevantes
      (distancia coseno ≤ 0.6) filtrados por cliente.
- [ ] Con `EMBEDDING_PROVIDER=none` todo funciona igual, solo sin recall (degradación
      explícita, sin errores).
- [ ] La identidad es por canal: `UNIQUE (channel, external_id)`; el mismo teléfono
      en otro canal es otra identidad, cruzable por `contact_phone`.

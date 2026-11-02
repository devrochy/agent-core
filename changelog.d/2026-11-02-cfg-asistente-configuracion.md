## Asistente de configuración por preguntas (2026-11-02)

- Configuración: `core/config_registro/asistente.py` traduce las respuestas del dueño a opciones del registro (SPEC-37). Dos secciones en la ola 1: «Cómo habla tu asistente» (tono, emojis, largo, frases prohibidas) y «Cuándo y cómo atiende» (horario, fuera de horario, velocidad, cuándo pasa a una persona).
- API: `POST /admin/settings/asistente/{seccion}/vista-previa` (valor actual → propuesto, sin escribir) y `POST .../aplicar` (exige la huella de la vista previa; 409 si cambió). Aplica todo o nada, en una sola transacción: configuración del negocio y persona del agente.
- `GET /admin/settings/catalog` trae el guion en `asistente`. Con `aos_runtime = v3` las preguntas que solo cambian la voz no se ofrecen, porque la voz sale de la release publicada.
- `save_spec_version` acepta una `session` opcional para guardar el AgentSpec dentro de la transacción de quien llama.

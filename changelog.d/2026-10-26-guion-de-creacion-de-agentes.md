## Crear un agente respondiendo un guion (2026-10-26)

- HU-AOS-48, SPEC-39 (D-49, ADR-0171). Cada plantilla de agente trae su guion de preguntas
  (`PlantillaAgente.preguntas`, en `core/presets/plantillas/*.json`): Ventas pregunta qué vende el negocio
  (productos, planes o ambos) y deja solo `tomar_pedido`, solo `contratar_plan` o los dos; todas preguntan el
  largo de las respuestas (`persona.maxLength`). Sin responder se aplica el valor por defecto del guion. Las
  respuestas solo cambian superpoderes, largo y nombre: ningún texto llega a un prompt.
- `GET /api/v1/admin/agent-templates` suma `questions` (`id`, `text`, `help`, `type`, `options[]`, `default`).
- Nuevo `POST /api/v1/admin/agents/from-script` `{template_key, answers, name?}`: crea el borrador (sin publicar)
  con los superpoderes de las respuestas y devuelve `agent`, `draft`, `warnings` y los `requirements` que le
  faltan al negocio. 422 `invalid_answers` con un campo por pregunta (no crea nada); 409 `key_taken` con el
  `agent_id` existente. Reutiliza `crear_desde_plantilla` (nuevo parámetro `respuestas`) y su guarda de enfoque.
- Sin migraciones. `docs/openapi.json` al día.

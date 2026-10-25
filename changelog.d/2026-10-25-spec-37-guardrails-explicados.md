## Cada guardrail del catálogo de Configuración trae su nombre legible y qué evita (SPEC-37) (2026-10-25)

- `register_guardrail(name, fn, priority, *, etiqueta="", explicacion="")`: el pack declara el texto para personas de su guardrail
  (`Guardrail` lo guarda); sin texto, el catálogo muestra el nombre como antes. Los cuatro guardrails de pack-cobranza, pack-calendar y
  pack-subscriptions ya lo declaran.
- Los tres interruptores del verificador (ADR-0119) los explica el core (`INFO_VERIFICADOR`).
- `GET /api/v1/admin/settings/catalog`: `disabled_guardrails.valores[]` trae `etiqueta` (legible) y `nota` (qué evita). La forma de la
  respuesta no cambia, así que `docs/openapi.json` tampoco. Son textos para personas: nunca entran al contexto del modelo.

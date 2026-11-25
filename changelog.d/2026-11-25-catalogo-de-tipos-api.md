## API del catálogo de tipos y habilidades (HU-AOS-94) (2026-11-25)

- Rutas nuevas: `GET /api/v1/admin/agent-types`, `GET /api/v1/admin/skill-catalog`, `GET /api/v1/admin/agents/{id}/posibles`
  y `POST /api/v1/admin/agent-types/{key}/agents`. Las lecturas piden la sección `agents`. `agent-templates` queda
  `deprecated` con la misma salida.
- `PUT /api/v1/admin/agents/{id}/skills/{pack}` responde 409 `habilidad_incompatible` si el tipo del agente no admite
  la transversal o el pack es de otra especialidad; no escribe nada.
- El manifiesto del tipo de agente suma `habilidades_transversales`; los cuatro packs suben de versión.

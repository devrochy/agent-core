## El General y el Coordinador no se archivan (2026-11-07)

- `PATCH /api/v1/admin/agents/{id}` con `status: "archived"` responde 409 `agent_reserved` para el General y el Coordinador, aunque el General ya no esté en ninguna conexión. Antes se podía archivar el General tras quitarlo de todas, y entonces las conexiones nuevas quedaban sin agentes (`core_missing`) y fallaban el backfill y los presets.
- Archivar un agente que sigue en una conexión responde 409 `agent_in_use` (antes `conflict`), para que el panel diga qué hacer. Reactivar (`status: "active"`) no cambia.
- Sin migración ni cambios en `docs/openapi.json`.

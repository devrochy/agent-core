## Horario por agente (2026-11-17, HU-AOS-82)

- Migración `0145`: `agent_drafts.schedule`, `agent_versions.schedule` y `schedule_hash` (nullable: sin backfill, todos heredan el del negocio). La unicidad de versiones pasa a un índice único `(tenant_id, agent_id, content_hash, flow_hash, schedule_hash)`: publicar un cambio solo de horario o solo de flujo ya no choca. Reversa solo sin horarios guardados.
- `core/agents/horario_agente.py`: validación (zona, días, HH:MM), huella y fallback al horario del negocio (uno guardado inválido nunca falla el turno).
- `GET/PUT /api/v1/admin/agents/{agent_id}/schedule` (con `lock_version`; `null` vuelve a heredar). Se congela al publicar y viaja en la release (`components[].schedule`).
- Ingreso (fuera de horario) y bloque del prompt (horario informativo) usan el horario del agente que atiende (dueño vigente o el de la conexión); el modo y el aviso siguen siendo del negocio. Sin horario propio el prompt no cambia. Los crons no lo miran.

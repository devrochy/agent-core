## Pausa por agente (HU-ARCH-38, segunda parte, 2026-11-16)

- Un especialista se pausa o reanuda con `POST /api/v1/admin/agents/{id}/pause` (`{reason}`) y `.../resume`, con el permiso de escritura de Configuración. Es estado vivo (`agents.paused_at`, `paused_by`, `paused_reason`, migración 0141): aplica en el siguiente turno, sin publicar una release (ADR-0200).
- Un especialista pausado no atiende: el turno pasa al agente por defecto sin cambiar el dueño de la conversación (al reanudar, vuelve a él). Si el de por defecto también está pausado, el turno se cierra en silencio con `kill_switch`.
- A2A niega delegar, pasar la conversación o consultar a un agente pausado (`agent_paused`, nueva razón en `a2a_events`), y el modelo ni lo ve entre los destinos.
- El Coordinador no se pausa (409 `agent_reserved`: para eso está la pausa del negocio) ni un agente archivado (409 `agent_archived`). Cada cambio queda en la auditoría del negocio y en `agentcore_kill_switch_changes_total{level="agent"}`.
- `AgentSummary` suma `paused`, `paused_at` y `paused_reason`. La pausa por agente no frena jobs: corren por negocio.

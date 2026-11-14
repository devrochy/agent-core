## Flujo de atención por agente (2026-11-14)

- `agent_drafts.flow` y `agent_versions.flow` (migración `0133`) con `flow_hash`: el flujo
  deja de ser solo del negocio y se versiona con el agente (ADR-0195, SPEC-46, HU-AOS-74).
- `GET/PUT /admin/agents/{id}/flow` y `GET .../flow/options`: validan secciones y acciones
  del agente; la semilla sale de los `flow.json` de sus packs.
- Publicar congela el flujo y su hash con la versión; las versiones anteriores quedan con
  `flow` NULL y siguen leyendo `flow_tenant`.
- Pendiente en HU-AOS-75: la release y el runtime todavía compilan `flow_tenant`.

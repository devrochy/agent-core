## Se borra la tabla agent_specs (2026-11-22, HU-AOS-91, parte E)

- La migración **0157** borra `agent_specs` y la columna `agent_versions.legacy_agent_spec_id`, junto con su FK y su índice. Ningún código las lee desde la parte D3. Lo autoriza ADR-0206 §9.
- **Guarda (decisión del dueño):** antes del DROP, la migración compara los `packs` del spec activo de cada negocio con las habilidades instaladas en sus agentes activos.
  - Si algún negocio perdería una habilidad, aborta y lista negocio y habilidades.
  - Para seguir hay dos caminos: instalarlas con `agent-core db packs-sin-instalacion --todos --aplicar`, o aceptar la pérdida con `AGENT_SPECS_DROP_ACEPTADO=1` en el entorno de la migración.
  - En producción, C2 y esta migración llegan juntas: la guarda impide perder habilidades sin que alguien lo mire.
- La migración requiere un rol con BYPASSRLS, igual que la 0152.
- **Downgrade:** recrea la tabla y la columna vacías, con el mismo DDL de antes. Los datos solo vuelven desde el respaldo, así que conviene sacarlo antes de correrla en cada entorno.
- **API:** `GET /admin/agents/{id}/versions` sigue trayendo el campo `legacy`, ahora siempre en `false`.
- **Precheck:** `packs-sin-instalacion` sigue sirviendo si la guarda frena la migración. Después de la 0157 ya no encuentra nada.

## Releases por conexión con rollback en un clic (2026-10-11)

- Agent OS (HU-AOS-14, ADR-0117): módulo `core/releases/` con build de una release inmutable por conexión
  (versiones exactas de agentes y habilidades, overrides de playbooks, secciones del negocio y campos de
  `AppSettings` del prompt, con `manifest_hash`), promoción total, rollback en un clic y caché en proceso.
- Release 1 por conexión conversacional desde el AgentSpec v2 activo: `agent-core db backfill-releases`.
- Sticky por episodio y revalidación de tareas al cambiar de release (`revalidated`, `cancelled`/`escalated`
  con `release_changed`), solo con `tenants.aos_runtime = 'v3'`. Sin cambio de conducta con `legacy`.
- API `/api/v1/admin/connections/{id}/releases`, `/releases/{rid}`, `/releases/{rid}/promote` y `/rollback`.
- Sync de `pack_versions` en el arranque del `api` (`agent-core db sync-packs`, función SECURITY DEFINER de la
  migración 0079) y lock de versiones de los packs en CI (`packages/pack-versions.lock.json`).

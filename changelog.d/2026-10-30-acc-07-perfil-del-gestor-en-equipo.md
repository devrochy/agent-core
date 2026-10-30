## Perfil del gestor en Equipo (2026-10-30)

- ADR-0176, HU-ACC-07. Hook opcional `BaseModule.collaborator_profiles(tenant_id, admin_ids)` (por lote, solo lectura) y modelo `PackProfile`: el core pide a los packs habilitados los perfiles de los colaboradores de la página y los agrega al listado, sin leer tablas de ningún pack. Una llamada por pack con todos los ids, en paralelo y con tope de 1 s; si un pack falla o se pasa, el listado sale sin sus perfiles (`log.warning`).
- `GET /api/v1/admin/collaborators` suma `pack_profiles` a cada cuenta (aditivo; vacío si no tiene perfil). El core descarta `resumen` y `edit` para quien no es dueño ni master ni tiene acceso a una sección del pack.
- Cobranza implementa el hook con tres consultas por lote: el perfil de gestor trae estatus, carteras `{id, nombre}` y permisos de campo activos (de 14), con `edit.kind = cobranza_gestor_permisos`. Sin migración.

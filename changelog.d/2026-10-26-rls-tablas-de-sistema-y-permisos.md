## Permisos de colaboradores y vigilancia de las tablas sin RLS (2026-10-26)

- `GET /api/v1/admin/collaborators/{id}/permissions`: un owner o tester solo lee cuentas vinculadas a su negocio activo;
  para cualquier otra responde 400 «admin no encontrado», igual que para un UUID inexistente. Antes, con el UUID de
  cualquier admin de la plataforma obtenía su acceso efectivo, incluidos sus permisos globales (hallazgo del auditor
  multitenant). El master no cambia.
- Test de integración nuevo (`aislamiento`): toda tabla de `public` con `tenant_id` y sin RLS habilitada y forzada debe
  estar en una lista explícita (hoy: `tenant_user`, `webhook_events`, `admin_permissions`, `tenant_invitations`,
  `qa_tester_datasets`). Una tabla nueva en ese estado hace fallar el CI.
- Documentación: `MULTITENANT.md` y el SKILL de `db-migration` listan las 5; ADR-0081 corrige que `tenant_invitations`
  no usa funciones SECURITY DEFINER.

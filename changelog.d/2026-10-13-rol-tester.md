## Rol tester: master sin Colaboradores, solo en test (2026-10-13)

- **SPEC-30, HU-QA-04, ADR-0134.** Un tester actúa como master en todo menos Colaboradores. Solo existe con
  `QA_TESTERS_ENABLED=true`, que se carga únicamente en test.
- **`core/admin/roles.py`** separa operar (`actua_como_master`) de gestionar cuentas (`role == "master"`). Pasan
  a la regla nueva:
  - los permisos por sección (`effective_access` y `permissions_for`);
  - las rutas de plataforma y las de Meta reservadas al master;
  - la escritura de agentes y releases;
  - cerrar un ciclo de pruebas.
- **Gestionar cuentas sigue siendo solo del master:** colaboradores, invitaciones a negocios, testers e
  importar el catálogo. Un owner no puede tocar ni invitar a un tester.
- **Migración 0088:** `admin_tenants()` incluye a los testers. Suma la tabla de sistema `qa_tester_invitations`.
- **Invitación de testers:**
  - API (solo master): `GET/POST /api/v1/admin/qa/testers` y `POST /api/v1/admin/qa/testers/{id}/revoke`.
  - El correo lleva un enlace de un solo uso, que también se devuelve al master.
  - Canje en `POST /api/v1/auth/accept-tester-invitation`, con el bloqueo progresivo del login.
- **Sin la variable**, un tester no entra al panel ni a la app de campo, y sus sesiones dejan de valer.
  `/me` expone `qa_testers_habilitados`.
- **CLI:** `agent-core admin create --role tester`, rechazado si el rol no está habilitado.

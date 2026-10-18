## P0 de seguridad del ciclo de pruebas: suspensión, permisos, sesión y propietarios (2026-10-17)

- **ADR-0136.** Cuatro defectos reportados por los testers en test (ciclo 2026-10-13).
- **Negocio suspendido sin acceso:** el dueño y los colaboradores de un negocio `suspended` no inician sesión
  (403 `tenant_suspended`, sin crear sesión) y una sesión abierta responde 403 en la API, el panel clásico y la
  app de campo; `POST /auth/logout` sigue funcionando. Con dos negocios solo se corta el suspendido; el master y
  el tester entran igual. `negocios_de_cuenta` (`core/shared/tenant.py`) es el único filtro.
- **Permisos revocados que se aplican:** migración `0090` agrega el nivel `none` (denegado, solo por negocio) a
  `admin_permissions`. «Revocar» una sección a un miembro guarda la denegación en vez de volver al default del rol;
  revocar un pack completo (`pack_cobranza`) deniega todas sus sub-secciones; la fila de una sub-sección manda
  sobre la del pack. `/admin/me` y `require()` lo reflejan en el siguiente pedido; `/me` expone también el acceso
  por pack. `grant` admite `access: "none"`; CLI `admin grant --access none --tenant <slug>`.
- **Cuenta desactivada sin sesión:** desactivar o cambiar la contraseña desde Colaboradores cierra sus sesiones en
  la misma transacción; reactivar no las resucita.
- **Propietarios pares y desvincular:** un owner solo gestiona colaboradores de su negocio (no a otro owner ni al
  real) y no crea otros propietarios; nuevo `POST /api/v1/admin/collaborators/{id}/unlink` (`{tenant}`) con las
  mismas guardas, sin dejar un negocio sin propietario. El tester conserva lo que podía con sus cuentas de prueba.
- Tests de integración `aislamiento` en `tests/integration/test_p0_seguridad_pruebas.py` y unitarios del resolutor.

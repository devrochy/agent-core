## Baja de cuentas de equipo (2026-11-15)

- **Equipo:** el menú de cada persona y su ficha ganan **Eliminar del equipo** (y
  **Reactivar cuenta** en las eliminadas). Solo se ofrece cuando la API lo permite
  (`can_delete` / `can_reactivate`), sin 403 por diseño.
- **Baja lógica (ADR-0198):** la cuenta deja de entrar y desaparece de Equipo; se revocan
  sesiones, vínculos, permisos e invitaciones. La fila `admins` conserva correo, usuario y
  nombre: el historial de cartera (`Pago.registrado_por`) y la auditoría siguen mostrando la
  identidad real.
- **Historial de Cobranza intacto:** el perfil `Gestor` se da de baja lógica (no se borra) y
  conserva sus carteras, pagos, visitas, promesas, gastos y notas. Al reactivar la cuenta se
  revive el MISMO perfil, con su historial.
- **Correo no reasignable:** un correo dado de baja no se reutiliza automáticamente. El alta
  avisa y pide reactivar la misma cuenta; reactivar es una acción explícita (master, o el
  dueño que la dio de baja), restaura los mismos negocios y roles (`deleted_links`) y vuelve
  a ocupar cupo del plan.
- **API:** `POST /admin/collaborators/{id}/delete`, `POST /admin/collaborators/{id}/reactivate`,
  `GET /admin/collaborators?eliminadas=1`; `CollaboratorItem` gana `deleted`, `can_delete` y
  `can_reactivate`, y `ExisteResponse` gana `eliminada`.
- **Migración 0135:** columnas de baja lógica en `admins` (tabla de sistema, sin RLS).

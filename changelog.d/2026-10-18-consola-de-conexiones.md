## Consola de conexiones (2026-10-18)

- HU-AOS-23 (#385). `GET /api/v1/admin/connections/console` (sección `connections`, paginado): por cada
  conexión del negocio (manuales, la canónica de cada número de Meta y el chat web), su Coordinador y sus
  agentes, con la versión que corre en la release activa y la última publicada. Trae además la release
  activa y la canary, la salud (con cuenta de Meta manda el estado de la cuenta, ADR-0122), el alcance de la
  pausa y el consumo de 7 días (turnos, conversaciones y tokens; `cost_usd` solo con acceso a `costs`).
- En el sobre: `runtime` y `routing` (`tenants.aos_routing`, solo lectura), el pass^k del último run del
  negocio, `can_edit` y los agentes publicados sin ninguna conexión (D-35).
- Activar, desactivar, marcar principal o eliminar una conexión manual o una cuenta de Meta deja una fila
  en `audit_log` (`connection.*` y `meta_connection.*`).
- Solo lectura y sin migración: usa los índices de 0071.

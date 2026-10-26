## Tablero de fiabilidad por conexión y versión (HU-AOS-29, backend)

Es la base de la página «Calidad» del panel. El diseño está en la adenda HU-AOS-29 de ADR-0117. Todas las rutas exigen la sección `agents`; el dinero solo sale con `costs` y el `conversation_id` solo con `conversations`.

- **`GET /api/v1/admin/reliability`.** Filtra por periodo, conexión, versión y carril, y devuelve:
  - turnos, conversaciones, escalamiento (por turno y por conversación), fallidos, turnos sin resultado, p95 de latencia y costo, en total y por agente;
  - rechazos del verificador por tipo, con los falsos positivos ya marcados;
  - completitud por procedimiento (tareas cerradas en el periodo);
  - consistencia vigente de cada conexión conversacional: el pass^4 por procedimiento de su versión activa, heredado por la cadena de versiones mientras no se vuelva a tocar.

  Bajo 20 turnos o 10 tareas, las tasas salen en `null` («sin datos suficientes»).
- **`GET /api/v1/admin/reliability/verifier`** lista los turnos con un rechazo. **`POST .../verifier/{turn_id}/false-positive`** (escritura en `agents`) lo marca como falso positivo: es idempotente, no se desmarca y responde 404 si el turno no tiene ese rechazo.
- **`GET /api/v1/admin/usage?group_by=connection|agent|a2a|day`.** Gasto del ledger sin evals ni generación. El modo `a2a` agrupa por quién pide, a quién y en qué modo.
- **`GET /api/v1/admin/evals/runs`.** Últimas corridas de evaluación con su versión y `pass_k_average`, sin su costo.
- **Migración 0108:** índices concurrentes para las métricas por versión y por fecha de cierre de tareas, y el índice único de los falsos positivos.

## Colaboración entre agentes: permisos, guarda y ledger A2A (HU-AOS-18)

Base de la colaboración entre agentes del Agent OS (adenda HU-AOS-18 de ADR-0116). Todavía no hay operaciones que la usen (`encargar`, `consultar` y el handoff llegan con sus HU), así que no cambia la conducta de ningún turno.

- **Grafo de permisos versionado** (`core/agents/grafo_a2a.py`):
  - quién puede pasar la conversación, encargar o consultar a quién, con límites por permiso;
  - publicar es idempotente por hash y está serializado por negocio;
  - `broadcast` y `collaborate` se rechazan (son internos del Coordinador), igual que la autollamada y los agentes de otro negocio;
  - los ciclos de handoff se devuelven como advertencia.
- **Congelado en la release:**
  - el manifiesto lleva `a2a_graph` solo si el negocio tiene grafo (sin grafo, el hash de las releases no cambia);
  - `connection_releases.graph_version_id` se escribe desde el manifiesto;
  - el build falla con `a2a_invalido` si un permiso pide más profundidad o presupuesto que el KernelSpec del Coordinador.
- **Guarda determinista** (`core/kernel/a2a.py`):
  - razones: `turn_closed`, `cycle`, `no_link`, `depth`, `repeat`, `calls` (las denegaciones cuentan), `owner_frozen` (el freno de estabilidad del router), `data_class` y `budget` (reserva el presupuesto del permiso);
  - cada decisión va a `a2a_events` y a la proyección `a2a_conversations` en una transacción propia;
  - `payload_hash` es un HMAC con una clave derivada de `APP_SECRET_KEY`, y del payload solo se guarda la forma;
  - un ciclo emite `a2a.loop.detected`;
  - sin cabecera del turno no se escribe nada.
- **Gasto:** `liquidar` lo registra como `usage_ledger` con `kind = 'agent_run'` y `a2a_call_id`. Cuenta como gasto del negocio y del agente, pero no como turno: `NO_SON_TURNOS` se separa de `NO_ES_ACTIVIDAD`.
- **Migración 0106:** CHECK de `a2a_events.reason` e índice parcial `(tenant_id, a2a_call_id)` en `usage_ledger`.
- **API** `/api/v1/admin/a2a`: `GET`/`PUT /graph`, `GET /graph/versions` y `GET /calls`. Requiere la sección `settings`; en negocios legacy, solo el master puede escribir.

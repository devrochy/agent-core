## Un gestor solo actúa sobre sus carteras (2026-11-07)

- agent-core#701 (seguridad, entre gestores del mismo negocio). La API de campo acota cada acción a las carteras que el token puede ver:
  - `/sync-offline/eventos`: el `carteraId` del evento debe ser una cartera visible (visita, pago, abono, gasto, trayectoria); el préstamo y el cliente deben ser de esa cartera; `cambio_cliente` exige un cliente de una cartera visible. Antes un gestor podía mover el saldo y la caja de la cartera de otro.
  - `DELETE …/pagos/{id}` y `…/abonos/{id}`: el movimiento debe ser de la cartera de la ruta (404 si no).
  - `POST …/visitas/no-pago`: valida `prestamoId` y `clienteId` contra la cartera (404) y un `clienteId` ausente ya no da 500 sino 400.
  - `GET /clientes/{id}/evidencia/{ev}`: solo si el cliente es de una cartera visible.
  - Un `cuotaId` que no es del préstamo ya no se enlaza al pago ni se marca pagada.
  - `GET …/clientes/{id}/prestamos` y `…/cuotas/{id}/detalle`: el cliente / el préstamo deben ser de la cartera de la ruta (404 si no).
  - Una fecha prometida mal formada da 400 sin dejar la visita a medias.
  - El HTML de la liquidación escapa el nombre de la cartera y el periodo.
- Propietario y master siguen viendo todas las carteras. Sin migración ni cambios en `docs/openapi.json`.

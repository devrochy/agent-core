## El negocio tampoco ve el modelo en Costos ni en el detalle del turno (2026-11-22, HU-BILL-24)

- `GET /admin/costs` devuelve `by_model` vacío a quien no actúa como master, y `GET /admin/turns/{id}` devuelve `costs[].model` en `null` (ADR-0210 §1). Lo encontró la revisión de seguridad de SPEC-51. El esquema no cambia.

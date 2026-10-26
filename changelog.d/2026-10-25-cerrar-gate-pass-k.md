## Gate de release por pass^k, parte 4: cerrar el gate (HU-AOS-21)

Con la bandera del negocio encendida, una versión que toca procedimientos no se activa hasta que se mide contra la versión activa. Diseño en la adenda «Parte 4» de ADR-0117.

- **Bandera por negocio,** apagada por defecto: `tenants.settings.evals.gate`. Solo el master la cambia, con `GET/PUT /api/v1/admin/platform/tenants/{id}/gate`, junto con el tope mensual (acotado a 100 USD). El cambio queda en `audit_log`.
  - La escritura es atómica dentro de `settings.evals`, sin pisar el resto.
  - Exige la base de evaluación configurada (409 `sin_base_de_eval`).
- **Activar una versión** que toca procedimientos y no tiene veredicto:
  - si la bandera está encendida, la respuesta es 409 `gate_pendiente`. En `details` vienen el estado de la última medición y `encolado`, y la medición se encola;
  - si la medición no encontró escenarios, pasa con `sin_medir` (decisión del dueño);
  - un veredicto aprobado que dejó procedimientos sin medir los anota en la promoción.
- **Tarea del worker** `core.releases.evaluar_candidata`:
  - corre `corredor.medir`, con límites de tiempo por encima del de la medición y sin `acks_late`;
  - un candado de Redis (`gate:corredor`) asegura que no corran dos mediciones a la vez: si está ocupado, la tarea se reprograma;
  - sin base de evaluación configurada, deja `fallida/sin_base_de_eval`.
- **Endpoints nuevos:** `POST .../releases/{rid}/evaluate`, que encola o responde sin medir, y `GET .../releases/{rid}/evaluation`, que devuelve el veredicto por procedimiento, lo que quedó sin medir y la última medición.
- **`core/provisioning/consumer.py`** bloquea la fila del negocio al reescribir `settings`, para no perder la bandera.

## Resumen: ventana de 7/15/30 días y serie diaria por agente (2026-10-29)

- `GET /api/v1/admin/dashboard/agents?dias=7|15|30` (ADR-0171, adenda). La ventana aplica a los KPIs,
  sus textos (`Últimos N días`, `los N días anteriores`) y la ventana previa; un valor fuera de
  7/15/30 devuelve 422.
- Cada agente trae `serie[]` diaria cero-rellena (`fecha`, `turnos`, `conversaciones`, `costo`) en la
  zona horaria del negocio y `ultima_actividad`. El costo de la serie es `null` sin la sección `costs`.
- Sin tablas ni migración: los campos son aditivos y una app vieja sigue recibiendo 7 días.

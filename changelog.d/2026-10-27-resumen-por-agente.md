## Resumen por agente: KPIs del negocio, de cada agente y de su módulo (2026-10-27)

- `GET /api/v1/admin/dashboard/agents` (ADR-0171). Devuelve los KPIs del negocio (conversaciones,
  escalaciones pendientes, tasa de escalamiento, consistencia, costo) y, por cada agente, su actividad
  (turnos, conversaciones, escalados, tareas abiertas, costo) con el valor de los últimos 7 días, el de
  los 7 previos, el delta y un destino semántico (`ruta` + `params`) para ver el detalle.
- Cada pack aporta los KPIs de su módulo con un `ResumenProvider` en `core/admin/resumen_registry.py`:
  Ventas (pedidos, ventas pagadas, por despachar, clientes nuevos), Agenda (citas de hoy, próximas,
  agendadas, canceladas), Cobranza (recaudo, cartera en mora, promesas) y Suscripciones (MRR, altas,
  bajas, cobros vencidos). Son del módulo completo, no solo de lo que creó el agente.
- Un KPI sin el permiso de su sección se omite (no sale en cero); un módulo que falla sale con `error`
  sin tumbar el resto. Sin tablas ni migración.

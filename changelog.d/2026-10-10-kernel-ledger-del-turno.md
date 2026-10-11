## Kernel del Coordinador: decisión y resultado de cada turno (2026-10-10)

- Cada turno deja una fila en `core_decisions` (al decidir) y otra en `turn_outcomes` (al cerrar) con el mismo
  `turn_id`, y su `trace_id`. Bloqueos por negocio pausado, cuota, presupuesto, conexión pausada (ADR-0122), horario o
  conversación escalada quedan `blocked` con su motivo, sin llamar al LLM (HU-AOS-10, adenda de ADR-0112).
- `messages`, `usage_ledger` y `audit_log` llevan el `turn_id` del turno que las causó (`usage_ledger` también el
  `trace_id`); nunca el de otro negocio.
- La traza del turno viaja a las tareas Celery en `traceparent`: la salida comparte `trace_id` con el turno.
- Nuevos `core/kernel/decisions.py`, `ingress.py` (cuota y presupuesto) y `egress.py` (un mensaje por turno). Sin cambio
  de conducta; se apaga con `KERNEL_LEDGER_ENABLED=false`.

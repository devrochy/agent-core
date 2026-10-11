## Estado de la conversación: tareas event-sourced y tarjeta de estado (2026-10-10)

- HU-AOS-04 (ADR-0114). `core/kernel/state.py`: tareas como `task_events` (fuente de verdad) +
  proyección `conversation_tasks`, escritas en la misma transacción con un único reductor puro;
  la reconstrucción desde los eventos da lo mismo que lo guardado, también a través de un
  `revalidated` entre versiones del procedimiento (`data.schema_version` en cada evento).
- `core/kernel/episodio.py`: inicio de episodio por la hora de los mensajes (720 min sin
  mensajes, hasta que llegue el KernelSpec); al empezar uno nuevo, las tareas sin actividad en
  todo el anterior quedan `cancelled` con `episode_expired`. Fija `conversations.episode_started_at`.
- `core/kernel/tarjeta.py` + playbook del kernel `core/playbooks/kernel/estado-conversacion.md`:
  tarjeta de estado en el bloque dinámico, justo antes de la fecha y hora (ADR-0091), con los
  valores como cadenas JSON escapadas. Solo con `tenants.aos_procedures` encendida; apagada, el
  prompt no cambia.
- La confirmación de un procedimiento vive en la tarea (`pending_confirmation`), no en
  `conversations.state`. Migración `0074`: `conversation_tasks.pending_confirmation` y `status_reason`.
- API: `GET /api/v1/admin/conversations/{id}/tasks` y `…/tasks/{task_id}/events` (sección
  `conversations`, 404 entre negocios).

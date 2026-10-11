## Seguimientos del kernel del Coordinador: ids del debounce y turno en Celery (2026-10-11)

- HU-AOS-10, #438. El turno que dispara el debounce registra en `core_decisions.inbound_message_ids` todos los
  mensajes de la ráfaga. Los ids viajan en una clave Redis nueva, `debounce:inbound:{tenant_id}:{conversation_id}`
  (mismo TTL y pipeline que el texto), que la purga del negocio barre y que sobrevive al reencolado por cupo
  saturado.
- HU-AOS-10, #439. Las tareas Celery que lanza un turno escriben con su `turn_id` (`messages`, `usage_ledger`,
  `audit_log`): el turno viaja en headers del mensaje y el worker lo fija al empezar cada tarea, con la misma guarda
  de negocio del default de columna. El reintento lo conserva; las tareas nuevas que encola una tarea no lo heredan.
- Sin migración, sin bandera nueva y sin cambio de conducta (gate en replay sin regrabar). Adenda en ADR-0112.

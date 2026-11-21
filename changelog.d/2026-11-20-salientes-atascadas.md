## Salientes atascadas: barrido y claim que falla cerrado (H3, 2026-11-20)

- Cron nuevo `core.outbound_sweeper` (cada 5 min, módulo Plataforma): reencola el primer
  outbound `queued` de cada conversación que lleva más de `OUTBOUND_SWEEP_MINUTOS` (10) sin tarea
  viva. Antes, si se perdía la tarea Celery de ese mensaje (fallo de `.delay`, excepción entre el
  claim y el envío, fallo del update a `sent`), toda la conversación quedaba sin respuestas para
  siempre. Tras `OUTBOUND_SWEEP_MAX_REENCOLADOS` (3) reencolados sin avance, lo marca `failed`,
  escala a una persona y sigue con el siguiente.
- "Tarea viva" = claim en `outbound:claim:*` o marca `outbound:programado:{tenant}:{mensaje}`, que
  ahora deja la tarea antes de un reintento con countdown o de una pausa anti-baneo.
- `_claim_outbound` falla cerrado: si Redis no responde, no se envía y la tarea reintenta
  (TRANSIENT). Antes enviaba igual, lo que con el barrido podía duplicar mensajes.
- Lo que falle entre el claim y el envío (credenciales, base, ritmo de Telegram) libera el claim y
  reintenta. Si el proveedor acepta pero falla el update a `sent`, queda la marca
  `outbound:enviado:*` y el siguiente intento solo registra el envío, sin repetirlo.
- Celery fija `visibility_timeout` explícito (`CELERY_VISIBILITY_TIMEOUT_S`, 4200 s): mayor que
  `task_time_limit` (300 s) y que el countdown más largo de la salida (QUOTA, 3600 s).
- Migración 0150: índice parcial `ix_messages_outbound_queued` (`tenant_id, conversation_id,
  created_at, id` donde `direction='outbound' AND status='queued'`), con `CONCURRENTLY`.

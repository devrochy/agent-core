## Centro de notificaciones del negocio: núcleo, API y push (2026-10-25)

- Nuevas tablas `notifications`, `notification_reads` y `notification_state` (migración 0107, RLS forzada): una fila por
  evento del negocio, con el estado de lectura y las preferencias por admin. ADR-0167 (enmienda ADR-0099 y ADR-0107 §1).
- `core.notifications`: `NotificationSpec` (categorías conversaciones/ventas/cobros/agenda/sistema; severidades
  info/success/warning/critical), `notify()` en savepoint que nunca propaga y `notify_standalone()`. Visibilidad por
  permiso explícito de sección; el master ve el negocio seleccionado.
- API `/api/v1/me/notifications`: lista con cursor, contador de no leídas, marcar leídas o todas, archivar,
  preferencias de push por categoría y silencio temporal.
- Push: handler core de `notification.created` idempotente por `push_sent_at`; `enviar_push` ahora envía en lotes de
  100 con `badge`, `priority`, `ttl` y canal de Android por categoría (sin categoría, el canal general de siempre).
- Tarea beat de retención: baja lógica a los 180 días.
- Disparadores del negocio migrados al centro (el push sale del mismo evento): escalación y escalación fallida,
  conexión pausada, token de Meta vencido o por vencer, calidad de número en rojo, mensaje fallido (DLQ), plan
  pagado sin activar, pagos sin aplicar o con monto distinto, suscripción pausada por mora o sin forma de pago,
  actividad de campo de cobranza y citas (a la persona asignada). Eventos nuevos: lead nuevo y pedido nuevo.
- Para `warning` y `critical` el push sale de inmediato (Celery, idempotente por `push_sent_at`); el outbox queda
  como red de seguridad (≤30 s). `notify_owners_push` se conserva solo para los avisos de facturación de la
  plataforma al vendedor.

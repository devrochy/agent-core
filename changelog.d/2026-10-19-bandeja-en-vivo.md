## Bandeja de conversaciones en vivo (HU-UX-15, SPEC-31)

- `GET /admin/conversations`:
  - cada chat trae `last_message` (hasta 160 caracteres), `last_message_at`, `last_message_sender` y su
    conexión (`connection_id`, `connection_label`, `connection_provider`, `connection_type`);
  - se ordena por actividad (último mensaje o, si no hay, el inicio) y acepta `?connection_id=`;
  - el último mensaje sale de un `LATERAL … LIMIT 1` sobre `ix_messages_tenant_conv_created`: sin N+1.
- `GET /admin/conversations/{id}/messages?after=<created_at>`: lo nuevo del hilo para el polling del panel.
  Re-entrega los 2 s anteriores al cursor (un mensaje commiteado tarde no se pierde) y el panel deduplica por
  `id`. Tope de 200 con `has_more`.
- `ConversationDetail` suma `escalation` (la activa, con `reason_code`/`reason_label` de HU-UX-17, resumen y
  asignado), `notes` (las del agente y las de personas) y `usage` (costo, tokens y turnos de
  `usage_ledger`). Cada `MessageItem` trae `tool_calls`: las herramientas del turno, en el primer mensaje del
  agente de ese turno.
- Tickets sobre la conversación (sección `conversations`, escritura):
  - `POST …/{id}/take`: la escalación activa queda `asignada` a quien la toma (`Escalation.assigned_admin_id`)
    y el agente se pausa en esa conversación. 409 `no_escalation` o `already_taken`;
  - `POST …/{id}/return-to-bot`: resuelve las escalaciones, deja la conversación abierta y reactiva el agente;
  - `POST …/{id}/notes`: nota interna con `source="human"` en `state.notes`, con un append atómico en SQL.
    No se envía al cliente ni entra al prompt.
- La lista y el detalle ya no muestran conversaciones ni mensajes con `deleted_at`.
- Sin migración: el índice `(tenant_id, conversation_id, created_at)` de `messages` ya existía (0003).

## Los mensajes del chat web ya no se quedan en `queued` (2026-10-20)

- El adaptador saliente del chat web devolvía el mismo id sintético (`web:<visitante>`) para todos los
  mensajes de una conversación. El índice único `uq_messages_tenant_external_message_id` hacía fallar al
  segundo envío al marcarlo `sent`, y ese mensaje y todos los siguientes de la conversación quedaban en
  `queued` (el widget igual los mostraba). Smoke de test 2026-10-20: en cada conversación web solo el
  primer saliente llegaba a `sent`. Ahora el id es único por mensaje.
- Los mensajes que ya quedaron en `queued` en test no se tocan: es solo su estado; el cliente los vio.

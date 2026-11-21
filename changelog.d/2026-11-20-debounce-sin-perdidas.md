## El debounce ya no pierde mensajes cuando el turno falla (H2, 2026-11-20)

- Un turno diferido (el que agrupa una ráfaga de mensajes) que falla ya no se traga: antes
  `_fire_conversation` registraba `debounce_fire_failed` y el mensaje se perdía, porque el
  payload ya se había borrado al disparar. Ahora el payload se restaura (se reencola con su texto
  y sus ids, igual que cuando el cupo del negocio está saturado), y el siguiente intento espera
  5 s y luego 10 s.
- Un `TurnTimeout` también se reencola y cuenta como intento. `TurnQueueTimeout` y el apagado del
  proceso reencolan sin contar.
- Al tercer fallo (`MAX_DELIVERIES`, el mismo tope del consumer) el turno va a la DLQ
  `agent_core:inbound:dlq` con la forma de un entrante, se ve en «Mensajes fallidos» y se puede
  reintentar con `POST /admin/dlq/{id}/retry`. El equipo recibe el aviso de mensaje fallido.
- La recuperación de conversaciones huérfanas corre además cada 5 minutos dentro del poller del
  debounce, no solo al arrancar la API. Solo mira inbound de entre ~5 minutos y 1 hora, salta las
  conversaciones con un turno diferido en curso y recupera cada mensaje una sola vez.
- Claves Redis nuevas, con el negocio primero para que las barra la purga:
  `debounce:intentos:{tenant}:{conv}`, `debounce:en_curso:{tenant}:{conv}` y
  `debounce:recuperado:{tenant}:{message_id}`. Sin migraciones.

## Canal Telegram: webhook de entrada (ADR-0199, PR core 3) (2026-11-16)

- `POST /webhooks/telegram/{public_id}` (`core/gateway/telegram/webhook.py`). El negocio sale solo de la ruta (`telegram_route_connection`) y se exige el secreto de la conexión en `X-Telegram-Bot-Api-Secret-Token`. Sin el encabezado, o con una forma inválida, responde 401 sin tocar la base; el cuerpo se lee después de verificar el secreto.
  - Respuestas: 401 con secreto malo, 404 con `public_id` desconocido, 413 con un cuerpo de más de 64 KiB y 503 con el kill-switch apagado o Redis caído.
  - Grupos, mensajes editados, stickers y JSON raro responden 200 sin procesar. Una conexión pausada o un negocio no operativo, también.
- Auditoría en la tabla de webhooks con `telegram:<bot_id>`, sin el texto. La idempotencia usa llaves globales que no dependen de `update_id` (Telegram lo elige al azar tras una semana sin updates): `tg:<bot_id>:in:<chat_id>:<message_id>` y `tg:<bot_id>:cb:<callback_query.id>`. Un reintento de Telegram no duplica.
- Sentry tapa también los locales con el texto y el nombre del cliente (`raw_body`, `update_`, `mensaje`, `caption`, `first_name`…).
- Un toque de botón se publica con su `interactive_id` y el título visible, y se contesta con `answerCallbackQuery` (2 s, best-effort).
- Métricas: `telegram_webhook_total{resultado}` y `telegram_webhook_duracion_seg`.
- `agentcore_channel_telegram.adapters.TelegramInbound`, expuesto por el entry point:
  - Solo procesa chats privados con una persona: texto (`/start` incluido), foto y nota de voz con su caption, y `callback_query`.
  - Los medios van como `file_id`; se descargan en el PR core 5.
- Consumer: una conversación de Telegram sin su conexión validada no se crea (falla cerrado, como Twilio).
- Avisos por evento: `telegram` es un tipo de texto sin ventana de 24 h. El negocio puede definirle un texto de respaldo y la cobranza también sale por ahí.

# agentcore-channel-telegram

Canal Telegram con bot propio por negocio (ADR-0199) para agent-core.

Aporta lo propio de la Bot API, sin estado y sin base de datos:

- `client.py`: cliente HTTP solo contra `api.telegram.org`. El token va en la ruta (`/bot<token>/método`), así que
  nunca se registra ni sale en un error: `redactar()` y `mascara()` lo tapan.
- `errors.py`: estado HTTP y `description` de la Bot API → categoría de `ProviderError` (la que lee `decide_retry`).
  Un 403 «bot was blocked» es `RECIPIENT`, nunca `OPT_OUT`: el bloqueo es reversible.
- `limites.py`: límites de la Bot API (texto, caption, `callback_data`, archivos, secreto del webhook) y el ritmo.
- `adapters.py`: `TelegramInbound` (secreto del webhook y `Update` → `InboundMessage`, solo chats privados, ids
  globales `tg:<bot_id>:in:<chat_id>:<message_id>` y `tg:<bot_id>:cb:<callback_query.id>`). El `OutboundAdapter` llega en el PR core 4.

El webhook, las tablas, el alta, la salud y el kill-switch (`TELEGRAM_HABILITADO`) viven en el core
(`core/gateway/telegram/`).

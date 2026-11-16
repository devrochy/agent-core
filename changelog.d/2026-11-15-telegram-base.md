## Canal Telegram: base (ADR-0199, PR core 1) (2026-11-15)

- Migración 0137:
  - Agrega la tabla `telegram_connections`, 1:1 con su conexión, con RLS forzada.
  - Agrega el índice único global `uq_channel_connections_telegram_bot_viva`, para que un bot vivo esté en un solo negocio.
  - Agrega la función `telegram_route_connection(public_id)` para el webhook.
- Nuevo paquete `packages/channel-telegram` (`agentcore_channel_telegram`):
  - `client.py`: cliente de la Bot API que no deja salir el token del bot.
  - `errors.py`: mapeo de errores. Un 403 de bot bloqueado es `RECIPIENT`, nunca una baja, y un 404 es `CONFIG`, no `AUTH`.
  - `limites.py`: límites de la API y secreto del webhook.
- Tipo de conexión `telegram`, con botones y lista inline. Queda oculto (`available=False`, fuera del selector) hasta el PR core 4. `canales_con` no cuenta los tipos ocultos para todos.
- Kill-switch `TELEGRAM_HABILITADO`, encendido por defecto. Solo lo lee `core/gateway/telegram/habilitado.py`, y `disponible_para` lo mira antes que `available`.
- Redacción del token del bot:
  - En las trazas de OTel se tapa `/bot<token>/` y `/file/bot<token>/`, solo en `api.telegram.org`.
  - En Sentry se tapa el patrón `<id>:<secreto>` y las claves `bot_token` y `webhook_secret`.
- Pruebas que usaban `"telegram"` como canal inválido: ahora usan `"canal_inexistente"`.

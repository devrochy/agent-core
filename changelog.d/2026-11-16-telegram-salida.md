## Canal Telegram: salida y voz para cualquier conexión (ADR-0199, PR core 4) (2026-11-16)

- `TelegramOutbound`:
  - Texto, que sale en partes de 4096 caracteres si es más largo.
  - Foto y nota de voz (`sendVoice`), por URL.
  - Botones y listas como teclado inline, una fila por opción. La lista no se degrada en el adaptador.
  - Devuelve el id global `tg:<bot_id>:out:<chat_id>:<message_id>`.
- `resolve_credentials`: una conversación de Telegram sale solo por su bot. Si el bot está pausado, borrado o el kill-switch está apagado, no se envía nada. Un bot nunca es respaldo de otra conversación.
- Ids de botón de más de 64 bytes: viajan como `t:<token>` (Redis, 7 días) y el webhook los devuelve al original. Tras el toque se quita el teclado.
- Ritmo de salida:
  - 25 mensajes por segundo por bot y 1 por segundo por chat.
  - Tras un 429, el bot espera los `retry_after` que pide Telegram.
- Errores de envío:
  - Un 401 deja la conexión `revoked` y un 409, `error`.
  - Un 403 («bot bloqueado») falla ese envío sin reintentos y **sin dar de baja** al cliente.
- **La voz no está atada a una conexión** (enmienda ADR-0197):
  - `CapacidadesCanal` suma `voz` (hoy QR y Telegram) y la política de `pack-voice` decide por esa capacidad, no por el proveedor.
  - Si un canal no sabe mandar audio, o lo rechaza, sale el texto ya escrito.
- Telegram queda **disponible en el catálogo y en el selector** para todos los negocios, salvo con el kill-switch apagado. El catálogo de mensajes lo ofrece en las opciones con botones.

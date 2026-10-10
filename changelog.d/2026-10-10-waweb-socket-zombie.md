## Canal WhatsApp por QR: el connector recicla los sockets zombie (2026-10-10)

- `WaWebSocket` expone `alive()`; `NeonizeSocket` lo implementa con `is_connected` / `is_logged_in`
  del cliente de Neonize (en `neonize.aioze` son corutinas: se esperan, bajo `_BRIDGE_LOCK`).
- El drain del manager consulta `alive()` cada 30 s mientras la sesión está conectada. Si lleva 60 s sin
  conexión, registra `waweb_socket_zombie`, manda una alerta `warning`, marca la sesión `disconnected` y
  recicla el socket con la sesión guardada. Antes, si el auto-reconnect interno de whatsmeow fallaba sin
  emitir `DisconnectedEv` (`Error sending close to websocket ... EOF`), la sesión seguía en `connected` y
  los mensajes entrantes se perdían en silencio (en test, 4 días desde el 2026-10-06 y otra vez el
  2026-10-10).
- Un negocio que se vinculó por QR dentro de la tarea actual ya no pierde la sesión al reconectar: antes,
  si la tarea había arrancado en `pending_qr`, cualquier reciclado (desconexión o zombie) borraba las
  credenciales y volvía a pedir QR.

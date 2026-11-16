## Canal Telegram: el agente pide el teléfono con un botón (ADR-0199 §14, HU-CONN-26) (2026-11-16)

- Tool nueva del core, `pedir_telefono`: manda al cliente de Telegram el botón «Compartir mi teléfono» (`request_contact`). El texto y la etiqueta los pone el agente.
  - Se ofrece en los negocios con una conexión de Telegram activa y el kill-switch encendido.
  - No envía nada si el canal de la conversación no lo permite (`canal_sin_pedir_contacto`) o si el cliente ya tiene teléfono (`ya_tiene_telefono`). Una llamada repetida no lo duplica (`ya_enviado`).
  - En el playground se simula.
- Capacidad nueva `pedir_contacto` en `CapacidadesCanal`, solo en `telegram`, y tipo interactivo `contacto` (`sender.contacto_valido`).
- Migración 0139: el CHECK `ck_messages_interactive_objeto` acepta `contacto`.
- `TelegramOutbound` lo manda como teclado de respuesta de un solo uso. Lo que el cliente comparte lo guarda el consumer en `contact_phone` (PR core 5).
- Los agentes ya publicados la reciben al republicar (HU-ARCH-34).

## Paquete del canal WhatsApp vía Twilio (2026-11-07)

- PR-2 de WhatsApp vía Twilio (ADR-0186, en redacción). Nuevo `packages/channel-twilio` (proveedor `twilio`, entry point `agent_core.channels`). **Todavía no se ofrece**: el tipo `whatsapp_twilio` sigue sin estar disponible hasta PR-3, y nada del core lo usa aún.
  - `signature.py`: `X-Twilio-Signature` (HMAC-SHA1 de la URL y los parámetros ordenados, y `bodySHA256` para JSON). Se verifica solo contra la URL registrada y guardada; sin URL, no valida.
  - `errors.py`: tabla de 32 códigos de Twilio → categoría de `decide_retry`, confirmada contra el diccionario oficial. Un código desconocido sale del estado HTTP y se loguea. Un timeout de lectura o escritura en un POST es AMBIGUOUS (Twilio no tiene header de idempotencia).
  - `client.py`, `senders.py` (Senders API v2) y `content.py` (Content API y aprobación de WhatsApp, validación de variables `{{1}}`…).
  - `adapters.py`: entrada (texto, media, botón y lista), salida (texto, imagen y `send_content_template`) y `normalize_status` para los status callbacks.

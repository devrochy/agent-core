# agentcore-channel-twilio

Canal WhatsApp vía Twilio, fase 1 BYOK (ADR-0186): el negocio pega su Account SID y su
Auth Token, y el core descubre los senders, registra los webhooks y sincroniza las
plantillas. El registry lo descubre por el entry_point `agent_core.channels`
(proveedor `twilio`). El tipo de conexión `whatsapp_twilio` sigue sin estar
disponible hasta que la bandera por negocio lo encienda (PR-3).

## Módulos

- `signature.py`: verificación de `X-Twilio-Signature` contra la URL **registrada y
  guardada**, nunca una reconstruida de los headers.
- `client.py`: cliente HTTP único (autenticación básica) para la REST 2010-04-01,
  Senders v2 y Content v1. Todo fallo sale como `ProviderError` ya clasificado.
- `errors.py`: la tabla de códigos de Twilio → categoría de `decide_retry`.
- `senders.py`: listar y leer senders y escribir sus webhooks (Senders API v2; la v1
  se depreca).
- `content.py`: plantillas de Content API (crear, listar, borrar, pedir aprobación de
  WhatsApp y leer su estado).
- `adapters.py`: `TwilioInbound` (firma y `parse_inbound`), `TwilioOutbound` (texto,
  imagen y `send_content_template`) y `normalize_status` para los status callbacks.

## Credenciales de la salida

`account_sid`, `auth_token`, `sender` (E.164) y, opcional, `status_callback_url`.
Las arma `resolve_credentials` (PR-3) desde `channel_connections`, cifradas.

## Reglas

- El Auth Token nunca se loguea ni va en un mensaje de error.
- Un timeout de lectura o de escritura es AMBIGUOUS (el mensaje pudo haber salido):
  no se reintenta a ciegas.
- Un código que no está en la tabla se clasifica por el estado HTTP (4xx permanente,
  5xx transitorio) y se loguea; nunca se le inventa un significado.
- Los botones y listas (`send_interactive`, PR-7i) todavía no: el adaptador hereda el
  `ChannelError` de la base.

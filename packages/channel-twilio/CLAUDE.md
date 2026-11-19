# agentcore-channel-twilio

Canal WhatsApp vía Twilio (ADR-0186) para agent-core.

Aporta lo propio de Twilio, sin estado y sin base de datos:

- `signature.py`: firma `X-Twilio-Signature` (HMAC-SHA1 de la URL registrada y los parámetros).
- `client.py`: cliente HTTP con auth básica, solo contra los hosts fijos de Twilio.
- `errors.py`: código de Twilio → categoría de `ProviderError` (la que lee `decide_retry`).
- `senders.py` y `content.py`: Senders API v2 y Content API (plantillas).
- `status.py`: `MessageStatus` del callback → estado del mensaje del core.
- `adapters.py`: `InboundAdapter` y `OutboundAdapter` que el registry descubre por el entry point
  `agent_core.channels`.

El webhook, las tablas, la salud y el polling de plantillas viven en el core (`core/gateway/twilio*`).
La conexión es de tipo `whatsapp_twilio`, que solo se abre por bandera de negocio.

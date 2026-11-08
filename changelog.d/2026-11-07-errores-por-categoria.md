## Errores de proveedor por categoría (2026-11-07)

- PR-1 de WhatsApp vía Twilio (ADR-0186, en redacción). Base sin cambio de comportamiento para Meta:
  - `ProviderError(ChannelError)` en `core/gateway/channels/base.py`, con `provider`, `code`, `http_status`, `category` y `retry_after` (segundos). Categorías: TRANSIENT, RATE_LIMIT, QUOTA, AMBIGUOUS, AUTH, CONFIG, WINDOW, OPT_OUT, RECIPIENT, CONTENT, PERMANENT y la interna POLICY (waweb). Sin categoría, sale del estado HTTP: 429 es rate limit, otro 4xx es permanente y un 5xx o la falta de respuesta es transitorio.
  - `GraphError` pasa a ser `ProviderError` (`provider="meta"`) con las mismas categorías de antes; `retry_after_ms` se conserva.
  - `decide_retry` decide por categoría: CONFIG, WINDOW, OPT_OUT, RECIPIENT y CONTENT fallan al primer intento como PERMANENT y AUTH; QUOTA reintenta a la hora; AMBIGUOUS reintenta una sola vez y no va a la DLQ.
  - `InboundAdapter.verify_request` acepta `url` (los adaptadores actuales la ignoran) y `OutboundAdapter.send_content_template` lanza `ChannelError` por defecto.
  - Un `GraphError` de un envío interactivo ya no cae en el `except ChannelError` que lo volvía permanente.

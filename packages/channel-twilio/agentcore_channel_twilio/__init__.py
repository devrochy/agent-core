"""Canal WhatsApp vía Twilio (`twilio`) — adaptadores y clientes de su API (ADR-0186).

El registry del core (`agent_core.channels`) carga este módulo y lee `PROVIDER`, `InboundAdapter`
y `OutboundAdapter`. El webhook, las tablas y los jobs viven en el core.

Credenciales que reciben los adaptadores (las arma `resolve_credentials` desde la conexión):

- `account_sid` (`AC…`) y `auth_token`: auth básica de la API y clave de la firma del webhook.
- `from`: el sender, `whatsapp:+E164`; o `messaging_service_sid` (`MG…`) si la conexión usa uno.
- `status_callback_url` (opcional): URL registrada para los estados de entrega de cada mensaje.
"""

from __future__ import annotations

from agentcore_channel_twilio.adapters import TwilioInbound, TwilioOutbound

PROVIDER = "twilio"
InboundAdapter = TwilioInbound
OutboundAdapter = TwilioOutbound

__all__ = ["PROVIDER", "InboundAdapter", "OutboundAdapter", "TwilioInbound", "TwilioOutbound"]

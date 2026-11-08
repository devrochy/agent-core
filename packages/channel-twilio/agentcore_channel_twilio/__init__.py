"""Canal WhatsApp vía Twilio (`twilio`) — adaptadores y clientes de la API (ADR-0186).

El registry del core (`agent_core.channels`) carga este módulo y lee
`PROVIDER`/`InboundAdapter`/`OutboundAdapter`. Registrar el proveedor no lo ofrece:
el tipo de conexión `whatsapp_twilio` sigue sin estar disponible hasta PR-3.
"""

from __future__ import annotations

from agentcore_channel_twilio.adapters import TwilioInbound, TwilioOutbound

PROVIDER = "twilio"
InboundAdapter = TwilioInbound
OutboundAdapter = TwilioOutbound

__all__ = ["PROVIDER", "InboundAdapter", "OutboundAdapter", "TwilioInbound", "TwilioOutbound"]

"""Canal WhatsApp Web no oficial (`waweb`) — adaptadores y handoff (ADR-0074).

El registry del core (`agent_core.channels`) carga este módulo y lee
`PROVIDER`/`InboundAdapter`/`OutboundAdapter`. El socket de Neonize NO vive acá:
corre en el proceso `connector` del core.
"""

from __future__ import annotations

from agentcore_channel_waweb.adapters import WaWebInbound, WaWebOutbound

PROVIDER = "waweb"
InboundAdapter = WaWebInbound
OutboundAdapter = WaWebOutbound

__all__ = ["PROVIDER", "InboundAdapter", "OutboundAdapter", "WaWebInbound", "WaWebOutbound"]

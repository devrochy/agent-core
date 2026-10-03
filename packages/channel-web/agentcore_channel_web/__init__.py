"""Canal de chat web embebible (`web`) — adaptador de salida (HU-CHAN-02).

El INBOUND del chat web no llega por webhook: el navegador llama al endpoint
público `POST /api/pack/…/web-chat/{key}/messages`, que valida dominio +
Turnstile y encola al mismo stream que los demás canales. Por eso este paquete
solo expone un `OutboundAdapter`: la respuesta del asistente ya quedó persistida
como `Message` (status `sent`) por la tarea de salida, y el widget la lee por
polling en `GET …/web-chat/{key}/messages`. El adaptador devuelve un id sintético
para no romper el contrato de `send_text`/`send_image`.
"""

from __future__ import annotations

from agentcore_channel_web.adapters import WebOutbound

PROVIDER = "web"
InboundAdapter = None  # el inbound lo maneja el router público, no un adapter
OutboundAdapter = WebOutbound

__all__ = ["PROVIDER", "InboundAdapter", "OutboundAdapter", "WebOutbound"]

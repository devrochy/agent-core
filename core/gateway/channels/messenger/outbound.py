"""Adapter de salida de Facebook Messenger (Send API).

`credentials` (armado por `core/gateway/sender.py::_resolve_meta_connection_
credentials`) trae, además de `access_token`, todo lo que
`meta_messenger.send_messenger_message` necesita para validar ventana de
servicio/Conversation Routing ANTES de llamar a Meta -- `OutboundAdapter`
mantiene la misma firma genérica que WhatsApp (`dict[str, str]`), así que
esos datos viajan como strings dentro del dict en vez de ensanchar la
interfaz compartida entre canales."""

from __future__ import annotations

import uuid
from datetime import datetime

from core.gateway.channels.base import OutboundAdapter
from core.gateway.meta_messenger import send_messenger_message
from core.shared.exceptions import ChannelError


class FacebookMessengerOutbound(OutboundAdapter):
    provider = "meta"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        page_id = credentials.get("page_id", "")
        access_token = credentials.get("access_token", "")
        if not page_id or not access_token:
            raise ChannelError("Conexión de Messenger sin credenciales (page_id/access_token)")

        psid = to_external_id.removeprefix("messenger:")
        expires_raw = credentials.get("service_window_expires_at", "")
        service_window_expires_at: datetime | None = None
        if expires_raw:
            try:
                service_window_expires_at = datetime.fromisoformat(expires_raw)
            except ValueError:
                service_window_expires_at = None

        connection_id_raw = credentials.get("connection_id", "")
        connection_id = uuid.UUID(connection_id_raw) if connection_id_raw else None
        version = credentials.get("graph_api_version") or "v21.0"

        return await send_messenger_message(
            page_id, psid, text, access_token, version, connection_id,
            service_window_expires_at=service_window_expires_at,
            human_confirmed=credentials.get("human_confirmed") == "true",
            conversation_routing_enabled=credentials.get("conversation_routing_enabled") == "true",
        )

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        # Fuera de alcance de este ticket (solo lib/meta/messenger/send.ts
        # -- texto). El envío automatizado del bot con imagen fuera de esta
        # tanda cae acá y falla explícito, no en silencio.
        raise ChannelError("Envío de imágenes por Messenger no implementado todavía")

"""Adapter de salida de Instagram: despacha a la ruta (a) o (b) según
`credentials["login_route"]` (armado por `sender.py::
_resolve_instagram_credentials`) -- mismo criterio que Messenger de pasar
el estado necesario (ventana de servicio, etc.) dentro del dict de
credenciales en vez de ensanchar la interfaz `OutboundAdapter` compartida
entre canales."""

from __future__ import annotations

import uuid
from datetime import datetime

from core.gateway.channels.base import OutboundAdapter
from core.gateway.meta_instagram import (
    send_instagram_message_route_a,
    send_instagram_message_route_b,
)
from core.shared.exceptions import ChannelError


class InstagramOutbound(OutboundAdapter):
    provider = "meta"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        login_route = credentials.get("login_route", "")
        access_token = credentials.get("access_token", "")
        if not access_token:
            raise ChannelError("Conexión de Instagram sin token")

        # to_external_id: "instagram:{ig_user_id}:{igsid}" -- se toma el
        # último componente, el igsid (ver fix de colisión de IGSID).
        igsid = to_external_id.rsplit(":", 1)[-1]

        expires_raw = credentials.get("service_window_expires_at", "")
        service_window_expires_at: datetime | None = None
        if expires_raw:
            try:
                service_window_expires_at = datetime.fromisoformat(expires_raw)
            except ValueError:
                service_window_expires_at = None
        human_confirmed = credentials.get("human_confirmed") == "true"

        connection_id_raw = credentials.get("connection_id", "")
        connection_id = uuid.UUID(connection_id_raw) if connection_id_raw else None
        version = credentials.get("graph_api_version") or "v21.0"

        if login_route == "ig_facebook_login":
            page_id = credentials.get("page_id", "")
            if not page_id:
                raise ChannelError("Conexión de Instagram (ruta a) sin page_id")
            return await send_instagram_message_route_a(
                page_id, igsid, text, access_token, version, connection_id,
                service_window_expires_at=service_window_expires_at, human_confirmed=human_confirmed,
            )

        ig_user_id = credentials.get("ig_user_id", "")
        if not ig_user_id:
            raise ChannelError("Conexión de Instagram (ruta b) sin ig_user_id")
        return await send_instagram_message_route_b(
            ig_user_id, igsid, text, access_token,
            service_window_expires_at=service_window_expires_at, human_confirmed=human_confirmed,
        )

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        raise ChannelError("Envío de imágenes por Instagram no implementado todavía")

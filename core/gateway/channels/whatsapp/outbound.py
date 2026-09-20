"""WhatsApp saliente vía Meta Cloud API (Graph API)."""

from __future__ import annotations

import httpx

from core.gateway.channels.base import OutboundAdapter
from core.gateway.meta_errors import classify_and_build_error
from core.shared.exceptions import ChannelError

GRAPH_API = "https://graph.facebook.com/v21.0"


class WhatsAppMetaOutbound(OutboundAdapter):
    provider = "meta"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        # external_id canónico: "whatsapp:+573001234567" -> Meta espera dígitos
        to = to_external_id.removeprefix("whatsapp:").lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        return await self._post(credentials, payload)

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        to = to_external_id.removeprefix("whatsapp:").lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": {"link": image_url, "caption": caption},
        }
        return await self._post(credentials, payload)

    @staticmethod
    async def _post(credentials: dict[str, str], payload: dict[str, object]) -> str:
        phone_number_id = credentials.get("phone_number_id", "")
        access_token = credentials.get("access_token", "")
        if not phone_number_id or not access_token:
            raise ChannelError("Conexión WhatsApp sin credenciales (phone_number_id/access_token)")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{GRAPH_API}/{phone_number_id}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            # Se re-lanza el GraphError CLASIFICADO (no un ChannelError
            # genérico): la tarea de envío (`core/tasks/outbound_send.py`)
            # necesita el `category`/`code`/`retry_after_ms` reales para
            # decidir si reintenta, cuándo, o si es un fallo permanente
            # (131047, re-engagement) que no vale la pena reintentar.
            try:
                body = response.json()
            except Exception:
                body = None
            raise classify_and_build_error(
                http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text
            )
        data = response.json()
        try:
            message_id = data["messages"][0]["id"]
            return str(message_id)
        except (KeyError, IndexError):
            return ""

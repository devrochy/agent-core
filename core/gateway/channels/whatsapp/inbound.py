"""WhatsApp entrante vía Meta Cloud API.

Verificación: HMAC-SHA256 del body crudo con el App Secret (X-Hub-Signature-256).
Identidad canónica del cliente: "whatsapp:+E164" (el wa_id de Meta llega en
dígitos pelados; se normaliza aquí — ADR-033 de agent-sale).

Medios: se toleran sin romper (se capturan id/tipo). El plugin
mod-media-processor los descarga y transcribe/analiza antes del orchestrator.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from core.gateway.channels.base import InboundAdapter, InboundMessage


class WhatsAppMetaInbound(InboundAdapter):
    provider = "meta"
    channel = "whatsapp"

    def verify_request(self, raw_body: bytes, headers: dict[str, str], secret: str) -> bool:
        signature = headers.get("x-hub-signature-256", "")
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[7:], expected)

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        messages: list[InboundMessage] = []
        if payload.get("object") != "whatsapp_business_account":
            return messages

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                contacts = {c.get("wa_id"): c for c in value.get("contacts", [])}

                for msg in value.get("messages", []):
                    wa_id = msg.get("from", "")
                    contact = contacts.get(wa_id, {})
                    name = contact.get("profile", {}).get("name")

                    content = ""
                    media: list[dict[str, str]] = []
                    msg_type = msg.get("type", "text")

                    if msg_type == "text":
                        content = msg.get("text", {}).get("body", "")
                    elif msg_type in ("audio", "image", "video", "document"):
                        media_obj = msg.get(msg_type, {})
                        media.append(
                            {
                                "type": media_obj.get("mime_type", msg_type),
                                "id": media_obj.get("id", ""),
                                "url": "",  # se resuelve con GET /{media_id} (requiere access token)
                            }
                        )
                        content = media_obj.get("caption", "")
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive", {})
                        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                        content = reply.get("title", "")
                    else:
                        continue  # tipos no soportados: sticker, location, etc.

                    messages.append(
                        InboundMessage(
                            provider=self.provider,
                            channel=self.channel,
                            external_message_id=msg.get("id", ""),
                            customer_external_id=f"whatsapp:+{wa_id}",
                            customer_name=name,
                            content=content,
                            connection_external_id=phone_number_id,
                            media=media,
                        )
                    )
        return messages

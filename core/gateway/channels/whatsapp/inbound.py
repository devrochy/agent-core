"""Entrante vía Meta Cloud API: WhatsApp, y también Messenger/Instagram
(comparten la misma App de Meta y la misma firma HMAC a nivel de App -- no
hay credenciales de firma distintas por producto).

Verificación: HMAC-SHA256 del body crudo con el App Secret (X-Hub-Signature-256).
Identidad canónica del cliente: "whatsapp:+E164" (el wa_id de Meta llega en
dígitos pelados; se normaliza aquí — ADR-033 de agent-sale). Para Messenger/
Instagram: "messenger:<PSID>" / "instagram:<IGSID>" (mismo criterio de
prefijo por canal).

Medios: se toleran sin romper (se capturan id/tipo). El plugin
mod-media-processor los descarga y transcribe/analiza antes del orchestrator.

Messenger/Instagram son SOLO de entrada en esta tanda: no hay
`OutboundAdapter` para esos canales todavía (`core/gateway/channels/registry.py`
indexa por `provider`, no por canal). Una conversación de esos canales se
crea y persiste normal; el intento de RESPONDER usa el adapter de WhatsApp
por compartir `provider="meta"`, falla con `ChannelError` (endpoint/formato
de id equivocado), y `core/gateway/sender.py` ya captura esa excepción hoy
(marca el mensaje `failed`, no rompe el turno) -- degradado, no un crash.
"""

from __future__ import annotations

from typing import Any

from core.gateway.channels.base import InboundAdapter, InboundMessage


class WhatsAppMetaInbound(InboundAdapter):
    provider = "meta"
    channel = "whatsapp"

    def verify_request(self, raw_body: bytes, headers: dict[str, str], secret: str) -> bool:
        from core.gateway.webhook_verify import verify_signature

        return verify_signature(raw_body, headers.get("x-hub-signature-256", ""), secret)

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        object_type = payload.get("object")
        if object_type == "page":
            return self._parse_messaging(payload, channel="messenger", id_prefix="messenger")
        if object_type == "instagram":
            return self._parse_messaging(payload, channel="instagram", id_prefix="instagram")
        if object_type != "whatsapp_business_account":
            return []

        messages: list[InboundMessage] = []
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

    def _parse_messaging(self, payload: dict[str, Any], *, channel: str, id_prefix: str) -> list[InboundMessage]:
        """Messenger/Instagram: `entry[].messaging[]` directo, sin `changes[]`."""
        messages: list[InboundMessage] = []
        for entry in payload.get("entry", []):
            entry_id = entry.get("id", "")
            for event in entry.get("messaging", []):
                message = event.get("message")
                if not message:
                    continue  # delivery/read receipts, postbacks sin texto: no soportados aún
                sender_id = event.get("sender", {}).get("id", "")
                content = message.get("text", "")
                media: list[dict[str, str]] = []
                for att in message.get("attachments", []):
                    payload_obj = att.get("payload", {})
                    if att.get("type") == "ig_post":
                        # El attachment legacy `share` fue eliminado el
                        # 1/feb/2026 -- un post de IG compartido por DM llega
                        # como `ig_post`, con media ID/URL/caption propios
                        # (leer solo `share` deja esto sin metadata).
                        media.append(
                            {"type": "ig_post", "id": payload_obj.get("id", ""), "url": payload_obj.get("url", "")}
                        )
                        if not content:
                            content = payload_obj.get("caption") or payload_obj.get("title") or ""
                    else:
                        media.append({"type": att.get("type", ""), "id": "", "url": payload_obj.get("url", "")})
                messages.append(
                    InboundMessage(
                        provider=self.provider,
                        channel=channel,
                        external_message_id=message.get("mid", ""),
                        customer_external_id=f"{id_prefix}:{sender_id}",
                        customer_name=None,
                        content=content,
                        connection_external_id=entry_id,
                        media=media,
                    )
                )
        return messages

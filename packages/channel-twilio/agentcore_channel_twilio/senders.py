"""Senders API v2 de Twilio: los números de WhatsApp de una cuenta (ADR-0186).

Solo v2 (`/v2/Channels/Senders`): Twilio depreca la v1 el 2026-09-01. Se usa para el alta
(listar los senders y elegir uno), para registrar los webhooks del sender y para la salud.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentcore_channel_twilio.client import MESSAGING_BASE, TwilioClient, require_sid

SENDERS_URL = f"{MESSAGING_BASE}/Channels/Senders"
# Estados en los que el sender puede enviar. `ONLINE:UPDATING` es un cambio de perfil en curso.
ONLINE_STATUSES = frozenset({"ONLINE", "ONLINE:UPDATING"})


@dataclass(frozen=True)
class Sender:
    sid: str
    sender_id: str  # `whatsapp:+E164`
    status: str
    display_name: str = ""
    webhook: dict[str, Any] = field(default_factory=dict)
    quality_rating: str = ""

    @property
    def e164(self) -> str:
        return self.sender_id.removeprefix("whatsapp:")

    @property
    def online(self) -> bool:
        return self.status in ONLINE_STATUSES

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> Sender:
        profile = body.get("profile") or {}
        properties = body.get("properties") or {}
        return cls(
            sid=str(body.get("sid") or ""),
            sender_id=str(body.get("sender_id") or ""),
            status=str(body.get("status") or "").upper(),
            display_name=str(profile.get("name") or ""),
            webhook=dict(body.get("webhook") or {}),
            quality_rating=str(properties.get("quality_rating") or ""),
        )


async def list_whatsapp_senders(client: TwilioClient) -> list[Sender]:
    rows = await client.paginate(
        SENDERS_URL,
        key="senders",
        op="senders.list",
        params={"Channel": "whatsapp", "PageSize": 50},
    )
    return [
        Sender.from_api(row)
        for row in rows
        if str(row.get("sender_id") or "").startswith("whatsapp:")
    ]


async def get_sender(client: TwilioClient, sender_sid: str) -> Sender:
    body = await client.get(f"{SENDERS_URL}/{require_sid(sender_sid, 'XE')}", op="senders.get")
    return Sender.from_api(body)


async def set_webhooks(
    client: TwilioClient, sender_sid: str, *, callback_url: str, status_callback_url: str
) -> Sender:
    """Apunta los mensajes entrantes y los estados del sender a nuestras URL, siempre por POST
    (Twilio solo admite POST en los status callbacks). Repetirlo con los mismos valores no cambia
    nada: el alta y «re-registrar webhooks» lo llaman sin miedo."""
    payload = {
        "webhook": {
            "callback_url": callback_url,
            "callback_method": "POST",
            "status_callback_url": status_callback_url,
            "status_callback_method": "POST",
        }
    }
    body = await client.post_json(
        f"{SENDERS_URL}/{require_sid(sender_sid, 'XE')}", payload, op="senders.set_webhooks"
    )
    return Sender.from_api(body)

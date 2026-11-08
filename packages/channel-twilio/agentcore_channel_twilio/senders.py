"""Senders API v2 de WhatsApp (`https://messaging.twilio.com/v2/Channels/Senders`).

La v1 se depreca el 2026-09-01: solo se usa la v2. Listar exige `Channel=whatsapp`, la
paginación va por `meta.next_page_url` y un sender se actualiza con `POST /{Sid}` y cuerpo
JSON (no PUT). Fuente: https://www.twilio.com/docs/whatsapp/api/senders (2026-11-07).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agentcore_channel_twilio.client import MESSAGING_V2_BASE, TwilioClient
from core.shared.exceptions import ChannelError

SENDERS_URL = f"{MESSAGING_V2_BASE}/Channels/Senders"
_SENDER_SID = re.compile(r"^XE[0-9a-fA-F]{32}$")


@dataclass(frozen=True)
class Sender:
    sid: str
    sender_id: str  # `whatsapp:+15551234567`
    status: str  # ONLINE, OFFLINE, CREATING, ONLINE:UPDATING, …
    display_name: str = ""
    webhook: dict[str, Any] = field(default_factory=dict)
    offline_reasons: list[dict[str, Any]] = field(default_factory=list)

    @property
    def e164(self) -> str:
        return self.sender_id.removeprefix("whatsapp:")

    @property
    def online(self) -> bool:
        """Solo un sender ONLINE envía. `ONLINE:UPDATING` (cambio de perfil en curso) sigue
        enviando."""
        return self.status.upper() in ("ONLINE", "ONLINE:UPDATING")

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Sender:
        profile = raw.get("profile") or {}
        webhook = raw.get("webhook") or {}
        reasons = raw.get("offline_reasons") or []
        return cls(
            sid=str(raw.get("sid") or ""),
            sender_id=str(raw.get("sender_id") or ""),
            status=str(raw.get("status") or ""),
            display_name=str(profile.get("name") or "") if isinstance(profile, dict) else "",
            webhook=webhook if isinstance(webhook, dict) else {},
            offline_reasons=[r for r in reasons if isinstance(r, dict)]
            if isinstance(reasons, list)
            else [],
        )


def _check_sid(sid: str) -> str:
    """El SID va en la ruta: solo se acepta el formato de Twilio (llega del panel)."""
    if not _SENDER_SID.match(sid or ""):
        raise ChannelError("SID de sender de Twilio inválido")
    return sid


async def list_senders(client: TwilioClient) -> list[Sender]:
    raw = await client.paginate(
        SENDERS_URL, "senders", params={"Channel": "whatsapp", "PageSize": "50"}
    )
    return [
        Sender.from_api(s) for s in raw if str(s.get("sender_id") or "").startswith("whatsapp:")
    ]


async def get_sender(client: TwilioClient, sid: str) -> Sender:
    return Sender.from_api(await client.request("GET", f"{SENDERS_URL}/{_check_sid(sid)}"))


async def update_webhook(
    client: TwilioClient,
    sid: str,
    *,
    callback_url: str,
    status_callback_url: str,
    fallback_url: str | None = None,
) -> Sender:
    """Escribe los webhooks del sender. El que tenía antes lo guarda quien llama
    (`previous_webhook`, PR-3) leyendo el sender primero."""
    webhook: dict[str, str] = {
        "callback_url": callback_url,
        "callback_method": "POST",
        "status_callback_url": status_callback_url,
        "status_callback_method": "POST",
    }
    if fallback_url:
        webhook |= {"fallback_url": fallback_url, "fallback_method": "POST"}
    raw = await client.request(
        "POST", f"{SENDERS_URL}/{_check_sid(sid)}", json={"webhook": webhook}
    )
    return Sender.from_api(raw)

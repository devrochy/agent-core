"""Recurso Message de Twilio (ADR-0186 §11): el precio de un mensaje enviado.

`price` llega en negativo y "may not be immediately available" (documentación del recurso): `None` mientras
Twilio no lo calcula. La documentación no aclara si incluye el cargo de Meta además de la tarifa de Twilio.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from agentcore_channel_twilio.client import TwilioClient, require_sid


@dataclass(frozen=True)
class PrecioDeMensaje:
    sid: str
    status: str
    price: Decimal | None
    price_unit: str


async def get_message_price(client: TwilioClient, message_sid: str) -> PrecioDeMensaje:
    body = await client.get(
        f"{client.account_url}/Messages/{require_sid(message_sid, 'SM')}.json", op="messages.get"
    )
    raw = body.get("price")
    try:
        price = abs(Decimal(str(raw))) if raw not in (None, "") else None
    except InvalidOperation:
        price = None
    return PrecioDeMensaje(
        sid=str(body.get("sid") or message_sid),
        status=str(body.get("status") or "").lower(),
        price=price,
        price_unit=str(body.get("price_unit") or "").lower(),
    )

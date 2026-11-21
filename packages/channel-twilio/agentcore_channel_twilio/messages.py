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


# Tope de entrantes que se leen por consulta: la reconciliación mira una ventana de minutos, no el historial.
MAX_ENTRANTES = 200


@dataclass(frozen=True)
class MensajeEntrante:
    """Un mensaje que el cliente le mandó al número (recurso Message con `direction = inbound`)."""

    sid: str
    from_: str
    to: str
    body: str
    num_media: int
    date_created: str


async def list_inbound_messages(
    client: TwilioClient, to: str, *, desde: str
) -> list[MensajeEntrante]:
    """Entrantes recibidos en `to` (`whatsapp:+E164`) desde el día `desde` (`AAAA-MM-DD`, UTC), los más nuevos primero.
    Una sola página de `MAX_ENTRANTES`: la API 2010 pagina con `next_page_uri`, y la ventana de la reconciliación es
    corta. Twilio filtra por día (`DateSent>=`); el filtro por hora lo hace quien llama con `date_created`."""
    body = await client.get(
        f"{client.account_url}/Messages.json",
        op="messages.list",
        params={"To": to, "DateSent>": desde, "PageSize": MAX_ENTRANTES},
    )
    entrantes = []
    for m in body.get("messages") or []:
        if str(m.get("direction") or "") != "inbound":
            continue
        try:
            num_media = int(m.get("num_media") or 0)
        except (TypeError, ValueError):
            num_media = 0
        entrantes.append(
            MensajeEntrante(
                sid=str(m.get("sid") or ""),
                from_=str(m.get("from") or ""),
                to=str(m.get("to") or ""),
                body=str(m.get("body") or ""),
                num_media=num_media,
                date_created=str(m.get("date_created") or ""),
            )
        )
    return entrantes


async def list_media(client: TwilioClient, message_sid: str) -> list[tuple[str, str]]:
    """`(url, content_type)` de cada adjunto, con la URL que manda el webhook (`…/Media/ME…`, sin `.json`)."""
    sid = require_sid(message_sid, "SM")  # acepta también `MM…` (mensajes con media)
    body = await client.get(f"{client.account_url}/Messages/{sid}/Media.json", op="media.list")
    adjuntos = []
    for item in body.get("media_list") or []:
        uri = str(item.get("uri") or "").removesuffix(".json")
        if uri:
            adjuntos.append((f"https://api.twilio.com{uri}", str(item.get("content_type") or "")))
    return adjuntos

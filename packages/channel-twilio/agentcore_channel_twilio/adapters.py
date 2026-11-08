"""Adaptadores de WhatsApp vía Twilio: entrada, salida y normalizador de estados (ADR-0186).

Credenciales que espera la salida (las arma `resolve_credentials` en PR-3):
- `account_sid` y `auth_token`: los de la cuenta del negocio (BYOK).
- `sender`: el número del sender en E.164 (`+15551234567`); se envía como `whatsapp:+…`.
- `status_callback_url` (opcional): la URL de estados registrada para la conexión.

El cliente se identifica como `whatsapp:+573001234567`, igual que con Meta: Twilio usa
ese mismo formato en `From`/`To`, así que no hay que traducirlo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from agentcore_channel_twilio import signature
from agentcore_channel_twilio.client import TwilioClient
from core.gateway.channels.base import (
    InboundAdapter,
    InboundMessage,
    OutboundAdapter,
    ProviderError,
)
from core.shared.exceptions import ChannelError
from core.shared.logger import get_logger

log = get_logger(__name__)

WHATSAPP_PREFIX = "whatsapp:"
_MAX_MEDIA = 10  # Twilio manda hasta 10 adjuntos por mensaje (`MediaUrl0`…`MediaUrl9`)


def whatsapp_address(number: str) -> str:
    """`+573001234567`, `573001234567` o `whatsapp:+573001234567` → `whatsapp:+573001234567`."""
    bare = number.strip().removeprefix(WHATSAPP_PREFIX).strip()
    if not bare:
        return ""
    return f"{WHATSAPP_PREFIX}{bare if bare.startswith('+') else '+' + bare}"


def _media_id(url: str) -> str:
    """El SID del adjunto (`ME…`) es el último tramo de `MediaUrlN`."""
    return url.rstrip("/").rsplit("/", 1)[-1] if url else ""


class TwilioInbound(InboundAdapter):
    provider = "twilio"
    channel = "whatsapp"

    def verify_request(
        self, raw_body: bytes, headers: dict[str, str], secret: str, *, url: str | None = None
    ) -> bool:
        """`secret` es el Auth Token de la conexión y `url` la URL registrada y guardada. Sin
        URL no se verifica: reconstruirla de los headers detrás del proxy no es confiable."""
        if not url:
            return False
        lowered = {k.lower(): v for k, v in headers.items()}
        return signature.is_valid(
            url,
            raw_body,
            lowered.get("x-twilio-signature", ""),
            secret,
            content_type=lowered.get("content-type", ""),
        )

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        """`payload` es el formulario del webhook de mensajes entrantes ya decodificado. Un
        callback de estado o un SMS no son mensajes de WhatsApp: lista vacía."""
        message_sid = str(payload.get("MessageSid") or "")
        sender = str(payload.get("From") or "")
        to = str(payload.get("To") or "")
        if not message_sid or not sender.startswith(WHATSAPP_PREFIX):
            return []
        if payload.get("MessageStatus") and str(payload.get("MessageStatus")) != "received":
            return []  # callback de estado que llegó a la URL de entrada

        media: list[dict[str, str]] = []
        try:
            num_media = min(int(payload.get("NumMedia") or 0), _MAX_MEDIA)
        except (TypeError, ValueError):
            num_media = 0
        for i in range(num_media):
            url = str(payload.get(f"MediaUrl{i}") or "")
            if not url:
                continue
            media.append(
                {
                    "type": str(payload.get(f"MediaContentType{i}") or ""),
                    "id": _media_id(url),
                    "url": url,
                }
            )

        # `ButtonPayload` es el id del botón (documentado). `ListId`/`ListTitle` no están en la
        # documentación de 2026-11-07: quedan de respaldo y se confirman con PR-7i.
        interactive_id = str(payload.get("ButtonPayload") or payload.get("ListId") or "")
        content = str(
            payload.get("Body") or payload.get("ButtonText") or payload.get("ListTitle") or ""
        )
        return [
            InboundMessage(
                provider=self.provider,
                channel=self.channel,
                external_message_id=message_sid,
                customer_external_id=whatsapp_address(sender),
                customer_name=str(payload.get("ProfileName") or "") or None,
                content=content,
                connection_external_id=to.removeprefix(WHATSAPP_PREFIX),
                media=media,
                interactive_id=interactive_id,
            )
        ]


# Estados de `Message.status` del core. Los intermedios de Twilio (queued, accepted, sending,
# scheduled) no avanzan nada: el mensaje ya está `queued` o `sent` en la base.
CoreStatus = Literal["sent", "delivered", "read", "failed"]
_STATUS_MAP: dict[str, CoreStatus] = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "undelivered": "failed",
    "failed": "failed",
}
_INTERMEDIATE = frozenset({"queued", "accepted", "sending", "scheduled", "receiving", "received"})


@dataclass(frozen=True)
class StatusUpdate:
    """Un callback de estado ya traducido a los estados del core."""

    external_message_id: str
    status: CoreStatus
    error_code: int | None = None
    error_message: str | None = None


def normalize_status(payload: dict[str, Any]) -> StatusUpdate | None:
    """Traduce el formulario del status callback. `None` si no hay nada que aplicar (estado
    intermedio, desconocido o sin `MessageSid`). Twilio no manda la hora del evento: el orden lo
    garantiza el rango de estados del core, que nunca retrocede de `delivered` a `sent`."""
    sid = str(payload.get("MessageSid") or "")
    raw = str(payload.get("MessageStatus") or "").lower()
    if not sid or not raw:
        return None
    status = _STATUS_MAP.get(raw)
    if status is None:
        if raw not in _INTERMEDIATE:
            log.warning("twilio_status_desconocido", message_sid=sid, status=raw)
        return None
    error_code: int | None = None
    try:
        error_code = int(payload["ErrorCode"]) if payload.get("ErrorCode") else None
    except (TypeError, ValueError):
        error_code = None
    error_message = str(payload.get("ErrorMessage") or "") or None
    if status != "failed":
        error_code, error_message = None, None
    return StatusUpdate(sid, status, error_code, error_message)


class TwilioOutbound(OutboundAdapter):
    provider = "twilio"
    transport: httpx.AsyncBaseTransport | None = None  # solo para los tests

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        return await self._send(credentials, to_external_id, {"Body": text})

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        fields = {"MediaUrl": image_url}
        if caption:
            fields["Body"] = caption
        return await self._send(credentials, to_external_id, fields)

    async def send_content_template(
        self,
        credentials: dict[str, str],
        to_external_id: str,
        content_sid: str,
        variables: dict[str, str],
    ) -> str:
        """Plantilla aprobada por su Content SID (`HX…`). Fuera de la ventana de 24 h es la
        única forma de escribir; dentro, también sirve para botones y listas (PR-7i)."""
        if not content_sid:
            raise ChannelError("Plantilla de Twilio sin Content SID")
        fields = {"ContentSid": content_sid}
        if variables:
            fields["ContentVariables"] = json.dumps(variables, ensure_ascii=False)
        return await self._send(credentials, to_external_id, fields)

    async def _send(
        self, credentials: dict[str, str], to_external_id: str, fields: dict[str, str]
    ) -> str:
        sender = whatsapp_address(credentials.get("sender", ""))
        to = whatsapp_address(to_external_id)
        if not sender or not to:
            raise ChannelError("Conexión Twilio sin número de envío o sin destinatario")
        client = TwilioClient.from_credentials(credentials, transport=self.transport)
        data = {"From": sender, "To": to, **fields}
        if callback := credentials.get("status_callback_url"):
            data["StatusCallback"] = callback
        body = await client.request("POST", client.api_url("Messages.json"), data=data)
        sid = str(body.get("sid") or "")
        if not sid:
            # Respondió 2xx: el mensaje pudo haber salido. Reintentarlo a ciegas lo duplicaría.
            raise ProviderError(
                "Twilio aceptó el envío pero no devolvió el SID del mensaje",
                provider="twilio",
                http_status=200,
                category="AMBIGUOUS",
            )
        return sid

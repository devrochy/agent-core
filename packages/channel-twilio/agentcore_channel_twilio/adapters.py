"""Adaptadores del canal `twilio` (ADR-0186).

`TwilioInbound` verifica la firma contra la URL exacta que se registró en el sender (la recibe el
webhook del core como `url`) y convierte el formulario de Twilio en `InboundMessage`. Sin `url`
no verifica nada: falla cerrado.

`TwilioOutbound` envía por `POST /Accounts/{AC}/Messages.json`. La identidad del cliente se
conserva como `whatsapp:+E164` también al enviar: Twilio la espera así (Meta, en cambio, quiere
los dígitos pelados).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl

import httpx

from agentcore_channel_twilio.client import TwilioClient, require_sid
from agentcore_channel_twilio.signature import validate_signature
from core.gateway.channels.base import (
    InboundAdapter,
    InboundMessage,
    OutboundAdapter,
    ProviderError,
)

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
# Tope de adjuntos que se leen de un mensaje: WhatsApp manda uno, Twilio documenta hasta 10.
_MAX_MEDIA = 10


def whatsapp_address(value: str) -> str:
    """`whatsapp:+E164` a partir de `whatsapp:+57…`, `+57…` o `57…`. Lanza `ProviderError`
    RECIPIENT si no es un número E.164."""
    number = str(value or "").strip().removeprefix("whatsapp:")
    if number and not number.startswith("+"):
        number = f"+{number}"
    if not _E164.match(number):
        raise ProviderError("Número de WhatsApp inválido", provider="twilio", category="RECIPIENT")
    return f"whatsapp:{number}"


def form_params(raw_body: bytes) -> list[tuple[str, str]]:
    """Parámetros del cuerpo `application/x-www-form-urlencoded`, en orden y con repetidos."""
    return parse_qsl(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)


class TwilioInbound(InboundAdapter):
    provider = "twilio"
    channel = "whatsapp"

    def verify_request(
        self, raw_body: bytes, headers: dict[str, str], secret: str, *, url: str | None = None
    ) -> bool:
        if not url or not secret:
            return False
        signature = next((v for k, v in headers.items() if k.lower() == "x-twilio-signature"), "")
        return validate_signature(secret, url, form_params(raw_body), signature)

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        """`payload` es el formulario del webhook de entrada (una clave, un valor). Lista vacía si
        no es un mensaje del cliente o si es un tipo que el core no procesa (ubicación sola),
        igual que con Meta."""
        message_sid = str(payload.get("MessageSid") or "")
        if not message_sid or not payload.get("From") or not payload.get("To"):
            return []
        try:
            customer = whatsapp_address(str(payload["From"]))
            connection = whatsapp_address(str(payload["To"])).removeprefix("whatsapp:")
        except ProviderError:
            return []

        media = self._media(payload, message_sid)
        content = str(payload.get("Body") or "")
        interactive_id = str(payload.get("ButtonPayload") or payload.get("ListId") or "")[:256]
        if not content:
            content = str(payload.get("ButtonText") or payload.get("ListTitle") or "")
        if not content and not media and not interactive_id:
            return []

        return [
            InboundMessage(
                provider=self.provider,
                channel=self.channel,
                external_message_id=message_sid,
                customer_external_id=customer,
                customer_name=str(payload.get("ProfileName") or "") or None,
                content=content,
                connection_external_id=connection,
                media=media,
                interactive_id=interactive_id,
            )
        ]

    @staticmethod
    def _media(payload: Mapping[str, Any], message_sid: str) -> list[dict[str, str]]:
        try:
            count = min(int(payload.get("NumMedia") or 0), _MAX_MEDIA)
        except (TypeError, ValueError):
            count = 0
        media = []
        for n in range(count):
            url = str(payload.get(f"MediaUrl{n}") or "")
            if url:
                media.append(
                    {
                        "type": str(payload.get(f"MediaContentType{n}") or ""),
                        "id": f"{message_sid}:{n}",
                        # Se descarga con auth básica de la conexión (`media/ingesta.py`).
                        "url": url,
                    }
                )
        return media


class TwilioOutbound(OutboundAdapter):
    provider = "twilio"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        return await self._send(credentials, to_external_id, [("Body", text)])

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        fields = [("MediaUrl", image_url)]
        if caption:
            fields.append(("Body", caption))
        return await self._send(credentials, to_external_id, fields)

    async def send_content_template(
        self,
        credentials: dict[str, str],
        to_external_id: str,
        content_sid: str,
        variables: dict[str, str],
    ) -> str:
        fields = [("ContentSid", require_sid(content_sid, "HX"))]
        if variables:
            fields.append(("ContentVariables", json.dumps(variables, ensure_ascii=False)))
        return await self._send(credentials, to_external_id, fields)

    async def _send(
        self, credentials: Mapping[str, str], to_external_id: str, fields: list[tuple[str, str]]
    ) -> str:
        client = TwilioClient.from_credentials(credentials, transport=self._transport)
        data = [("To", whatsapp_address(to_external_id)), *self._sender(credentials), *fields]
        if credentials.get("status_callback_url"):
            data.append(("StatusCallback", str(credentials["status_callback_url"])))
        body = await client.post_form(
            f"{client.account_url}/Messages.json", data, op="messages.create"
        )
        sid = str(body.get("sid") or "")
        if not sid:
            raise ProviderError(
                "Twilio no devolvió el id del mensaje", provider="twilio", category="AMBIGUOUS"
            )
        return sid

    @staticmethod
    def _sender(credentials: Mapping[str, str]) -> list[tuple[str, str]]:
        if credentials.get("messaging_service_sid"):
            return [
                (
                    "MessagingServiceSid",
                    require_sid(str(credentials["messaging_service_sid"]), "MG"),
                )
            ]
        if credentials.get("from"):
            try:
                return [("From", whatsapp_address(str(credentials["from"])))]
            except ProviderError as exc:
                raise ProviderError(
                    "El número de la conexión de Twilio no es válido",
                    provider="twilio",
                    category="CONFIG",
                ) from exc
        raise ProviderError(
            "La conexión de Twilio no tiene número ni Messaging Service",
            provider="twilio",
            category="CONFIG",
        )

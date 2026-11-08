"""Adaptadores de WhatsApp vía Twilio (ADR-0186): `parse_inbound`, normalizador de estados y
envío con `httpx.MockTransport` (sin red)."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

import agentcore_channel_twilio
import httpx
import pytest
from agentcore_channel_twilio.adapters import (
    StatusUpdate,
    TwilioInbound,
    TwilioOutbound,
    normalize_status,
    whatsapp_address,
)

from core.gateway.channels.base import ProviderError
from core.shared.exceptions import ChannelError

SID = "AC" + "a" * 32
CREDS = {"account_sid": SID, "auth_token": "token-secreto", "sender": "+15551234567"}
BASE_IN = {
    "MessageSid": "SM" + "1" * 32,
    "AccountSid": SID,
    "From": "whatsapp:+573001234567",
    "To": "whatsapp:+15551234567",
    "ProfileName": "Ana",
    "WaId": "573001234567",
    "SmsStatus": "received",
}


# --- entrada ----------------------------------------------------------------


def test_texto():
    [msg] = TwilioInbound().parse_inbound({**BASE_IN, "Body": "Hola", "NumMedia": "0"})
    assert (msg.provider, msg.channel) == ("twilio", "whatsapp")
    assert msg.external_message_id == BASE_IN["MessageSid"]
    assert msg.customer_external_id == "whatsapp:+573001234567"
    assert msg.customer_name == "Ana"
    assert msg.content == "Hola"
    assert msg.connection_external_id == "+15551234567"
    assert msg.media == [] and msg.interactive_id == ""


def test_media():
    url = f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages/SMx/Media/ME{'2' * 32}"
    [msg] = TwilioInbound().parse_inbound(
        {**BASE_IN, "Body": "", "NumMedia": "2", "MediaUrl0": url, "MediaContentType0": "image/jpeg",
         "MediaUrl1": url + "b", "MediaContentType1": "audio/ogg"}
    )  # fmt: skip
    assert msg.media[0] == {"type": "image/jpeg", "id": "ME" + "2" * 32, "url": url}
    assert msg.media[1]["type"] == "audio/ogg"
    assert len(msg.media) == 2


def test_num_media_invalido_no_rompe():
    [msg] = TwilioInbound().parse_inbound({**BASE_IN, "Body": "x", "NumMedia": "muchos"})
    assert msg.media == []


def test_boton_y_lista():
    [boton] = TwilioInbound().parse_inbound(
        {**BASE_IN, "Body": "Premium", "ButtonText": "Premium", "ButtonPayload": "plan:premium"}
    )
    assert (boton.content, boton.interactive_id) == ("Premium", "plan:premium")
    [lista] = TwilioInbound().parse_inbound({**BASE_IN, "ListId": "fila:2", "ListTitle": "Martes"})
    assert (lista.content, lista.interactive_id) == ("Martes", "fila:2")


@pytest.mark.parametrize(
    "payload",
    [
        {**BASE_IN, "From": "+573001234567", "Body": "un SMS"},
        {**BASE_IN, "MessageSid": "", "Body": "x"},
        {**BASE_IN, "MessageStatus": "delivered"},
        {},
    ],
)
def test_lo_que_no_es_un_mensaje_de_whatsapp_da_lista_vacia(payload):
    assert TwilioInbound().parse_inbound(payload) == []


# --- estados ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "esperado"),
    [("sent", "sent"), ("delivered", "delivered"), ("read", "read"), ("undelivered", "failed"),
     ("failed", "failed"), ("DELIVERED", "delivered")],
)  # fmt: skip
def test_estados_que_avanzan(raw, esperado):
    update = normalize_status({"MessageSid": "SM1", "MessageStatus": raw})
    assert update == StatusUpdate("SM1", esperado)


@pytest.mark.parametrize("raw", ["queued", "accepted", "sending", "scheduled", "canceled", ""])
def test_estados_intermedios_o_desconocidos_no_aplican(raw):
    assert normalize_status({"MessageSid": "SM1", "MessageStatus": raw}) is None


def test_fallo_con_codigo_y_sin_sid():
    update = normalize_status(
        {"MessageSid": "SM1", "MessageStatus": "undelivered", "ErrorCode": "63016",
         "ChannelStatusMessage": "fuera de ventana"}
    )  # fmt: skip
    assert (update.status, update.error_code) == ("failed", 63016)
    assert normalize_status({"MessageStatus": "delivered"}) is None
    # Un código en un estado que no es fallo no se arrastra.
    assert (
        normalize_status(
            {"MessageSid": "SM1", "MessageStatus": "sent", "ErrorCode": "1"}
        ).error_code
        is None
    )


# --- salida -----------------------------------------------------------------


def _outbound(handler) -> tuple[TwilioOutbound, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    adapter = TwilioOutbound()
    adapter.transport = httpx.MockTransport(_h)
    return adapter, seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"sid": "SM" + "9" * 32, "status": "queued"})


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.mark.asyncio
async def test_send_text_arma_el_pedido():
    adapter, seen = _outbound(_ok)
    sid = await adapter.send_text(
        {**CREDS, "status_callback_url": "https://h/status"}, "whatsapp:+573001234567", "Hola"
    )
    assert sid == "SM" + "9" * 32
    [req] = seen
    assert req.method == "POST"
    assert str(req.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    assert (
        req.headers["authorization"]
        == "Basic " + base64.b64encode(f"{SID}:token-secreto".encode()).decode()
    )
    assert _form(req) == {
        "From": "whatsapp:+15551234567", "To": "whatsapp:+573001234567", "Body": "Hola",
        "StatusCallback": "https://h/status",
    }  # fmt: skip


@pytest.mark.asyncio
async def test_send_image_y_plantilla():
    adapter, seen = _outbound(_ok)
    await adapter.send_image(CREDS, "+573001234567", "https://img/x.jpg", "Foto")
    await adapter.send_image(CREDS, "573001234567", "https://img/y.jpg")
    await adapter.send_content_template(
        CREDS, "whatsapp:+573001234567", "HX" + "c" * 32, {"1": "Ana Núñez"}
    )
    imagen, sin_texto, plantilla = (_form(r) for r in seen)
    assert imagen["MediaUrl"] == "https://img/x.jpg" and imagen["Body"] == "Foto"
    assert imagen["To"] == sin_texto["To"] == "whatsapp:+573001234567"
    assert "Body" not in sin_texto
    assert plantilla["ContentSid"] == "HX" + "c" * 32
    assert json.loads(plantilla["ContentVariables"]) == {"1": "Ana Núñez"}
    assert "Body" not in plantilla


@pytest.mark.asyncio
async def test_credenciales_incompletas():
    adapter, seen = _outbound(_ok)
    with pytest.raises(ChannelError):
        await adapter.send_text({**CREDS, "sender": ""}, "whatsapp:+573001234567", "x")
    with pytest.raises(ChannelError):
        await adapter.send_text({**CREDS, "auth_token": ""}, "whatsapp:+573001234567", "x")
    with pytest.raises(ChannelError):
        await adapter.send_content_template(CREDS, "whatsapp:+573001234567", "", {})
    assert seen == []


@pytest.mark.asyncio
async def test_error_de_twilio_sale_clasificado():
    adapter, _ = _outbound(
        lambda r: httpx.Response(
            400, json={"code": 21610, "message": "unsubscribed", "status": 400}
        )
    )
    with pytest.raises(ProviderError) as info:
        await adapter.send_text(CREDS, "whatsapp:+573001234567", "x")
    assert (info.value.code, info.value.category) == (21610, "OPT_OUT")


@pytest.mark.asyncio
async def test_timeout_de_lectura_es_ambiguo():
    def _timeout(request):
        raise httpx.ReadTimeout("lento", request=request)

    adapter, _ = _outbound(_timeout)
    with pytest.raises(ProviderError) as info:
        await adapter.send_text(CREDS, "whatsapp:+573001234567", "x")
    assert info.value.category == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_2xx_sin_sid_o_sin_json_es_ambiguo():
    adapter, _ = _outbound(lambda r: httpx.Response(201, json={"status": "queued"}))
    with pytest.raises(ProviderError) as info:
        await adapter.send_text(CREDS, "whatsapp:+573001234567", "x")
    assert info.value.category == "AMBIGUOUS"
    adapter, _ = _outbound(lambda r: httpx.Response(201, text="ok"))
    with pytest.raises(ProviderError) as info:
        await adapter.send_text(CREDS, "whatsapp:+573001234567", "x")
    assert info.value.category == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_interactivos_y_plantillas_de_meta_todavia_no():
    adapter, _ = _outbound(_ok)
    with pytest.raises(ChannelError):
        await adapter.send_interactive(CREDS, "whatsapp:+573001234567", {"type": "button"})
    with pytest.raises(ChannelError):
        await adapter.send_template(CREDS, "whatsapp:+573001234567", "aviso", "es", [])


def test_whatsapp_address():
    assert whatsapp_address("+57300") == whatsapp_address("57300") == "whatsapp:+57300"
    assert whatsapp_address("whatsapp:+57300") == "whatsapp:+57300"
    assert whatsapp_address("  ") == ""


def test_el_modulo_expone_lo_que_lee_el_registry(monkeypatch):
    from core.gateway.channels import registry

    assert agentcore_channel_twilio.PROVIDER == "twilio"
    monkeypatch.setattr(registry, "_INBOUND", dict(registry._INBOUND))
    monkeypatch.setattr(registry, "_OUTBOUND", dict(registry._OUTBOUND))
    registry._register(
        agentcore_channel_twilio.PROVIDER,
        agentcore_channel_twilio.InboundAdapter(),
        agentcore_channel_twilio.OutboundAdapter(),
    )
    assert isinstance(registry._INBOUND["twilio"], TwilioInbound)
    assert isinstance(registry._OUTBOUND["twilio"], TwilioOutbound)

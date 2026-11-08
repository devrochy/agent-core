"""Clientes de Senders v2, Content y la cuenta (ADR-0186), con `httpx.MockTransport`."""

from __future__ import annotations

import json

import httpx
import pytest
from agentcore_channel_twilio import content, senders
from agentcore_channel_twilio.client import TwilioClient

from core.gateway.channels.base import ProviderError
from core.shared.exceptions import ChannelError

SID = "AC" + "a" * 32
XE = "XE" + "b" * 32
HX = "HX" + "c" * 32


def _client(handler) -> tuple[TwilioClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return TwilioClient(SID, "tok", transport=httpx.MockTransport(_h)), seen


def _sender(sid=XE, number="+15551234567", status="ONLINE") -> dict:
    return {
        "sid": sid,
        "sender_id": f"whatsapp:{number}",
        "status": status,
        "profile": {"name": "Solvershub"},
        "webhook": {"callback_url": "https://viejo/in"},
        "offline_reasons": None,
    }


def test_cliente_sin_credenciales():
    with pytest.raises(ChannelError):
        TwilioClient("", "tok")
    with pytest.raises(ChannelError):
        TwilioClient.from_credentials({"account_sid": SID})


@pytest.mark.asyncio
async def test_listar_senders_pagina_y_filtra_whatsapp():
    pagina2 = "https://messaging.twilio.com/v2/Channels/Senders?Channel=whatsapp&PageToken=p2"

    def handler(request):
        if "PageToken" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "senders": [_sender("XE" + "d" * 32, "+15550000000", "OFFLINE")],
                    "meta": {"next_page_url": None},
                },
            )
        return httpx.Response(
            200,
            json={
                "senders": [
                    _sender(),
                    {"sid": "XE" + "e" * 32, "sender_id": "rcs:x", "status": "ONLINE"},
                ],
                "meta": {"next_page_url": pagina2},
            },
        )

    client, seen = _client(handler)
    result = await senders.list_senders(client)
    assert [s.e164 for s in result] == ["+15551234567", "+15550000000"]
    assert [s.online for s in result] == [True, False]
    assert result[0].display_name == "Solvershub"
    assert seen[0].url.params["Channel"] == "whatsapp"
    assert str(seen[1].url) == pagina2


@pytest.mark.asyncio
async def test_paginacion_con_tope_de_paginas():
    client, seen = _client(
        lambda r: httpx.Response(
            200,
            json={"senders": [], "meta": {"next_page_url": "https://messaging.twilio.com/v2/x"}},
        )
    )
    await client.paginate(senders.SENDERS_URL, "senders", max_pages=3)
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_actualizar_webhook_es_post_json():
    client, seen = _client(lambda r: httpx.Response(200, json=_sender()))
    sender = await senders.update_webhook(
        client, XE, callback_url="https://core/in", status_callback_url="https://core/status"
    )
    [req] = seen
    assert (req.method, str(req.url)) == ("POST", f"{senders.SENDERS_URL}/{XE}")
    assert json.loads(req.content) == {
        "webhook": {
            "callback_url": "https://core/in",
            "callback_method": "POST",
            "status_callback_url": "https://core/status",
            "status_callback_method": "POST",
        }
    }
    assert sender.sid == XE


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["", "XE123", "../Accounts", "XE" + "z" * 32])
async def test_sid_de_sender_invalido_no_sale_a_red(sid):
    client, seen = _client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ChannelError):
        await senders.get_sender(client, sid)
    assert seen == []


@pytest.mark.asyncio
async def test_cuenta():
    client, seen = _client(
        lambda r: httpx.Response(200, json={"sid": SID, "status": "active", "type": "Full"})
    )
    assert (await client.fetch_account())["status"] == "active"
    assert str(seen[0].url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}.json"


@pytest.mark.asyncio
async def test_credenciales_rechazadas_al_leer_la_cuenta():
    client, _ = _client(
        lambda r: httpx.Response(401, json={"code": 20003, "message": "Authenticate"})
    )
    with pytest.raises(ProviderError) as info:
        await client.fetch_account()
    assert info.value.category == "AUTH"


# --- Content ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "variables", "problemas"),
    [
        ("Hola {{1}}, tu cita es el {{2}}", {"1": "Ana", "2": "martes"}, 0),
        ("Sin variables", {}, 0),
        ("Hola {{1}} y {{3}}", {"1": "a", "3": "c"}, 1),
        ("Hola {{1}}", {}, 1),
        ("Hola {{1}}", {"1": " "}, 1),
        ("Hola {{nombre}}", {}, 1),
        ("Hola {{1}}", {"1": "a", "2": "b"}, 1),
        ("{{ 1 }} y {{1}}", {"1": "a"}, 0),
    ],
)
def test_validar_variables(body, variables, problemas):
    assert len(content.validate_variables(body, variables)) == problemas


@pytest.mark.asyncio
async def test_crear_y_pedir_aprobacion():
    client, seen = _client(lambda r: httpx.Response(201, json={"sid": HX}))
    creado = await content.create_content(
        client, friendly_name="aviso_cita_v1", language="es",
        types={"twilio/text": {"body": "Hola {{1}}"}}, variables={"1": "Ana"},
    )  # fmt: skip
    await content.request_whatsapp_approval(client, HX, name="aviso_cita_v1", category="utility")
    assert creado["sid"] == HX
    crear, aprobar = seen
    assert str(crear.url) == "https://content.twilio.com/v1/Content"
    assert json.loads(crear.content)["variables"] == {"1": "Ana"}
    assert (
        str(aprobar.url) == f"https://content.twilio.com/v1/Content/{HX}/ApprovalRequests/whatsapp"
    )
    assert json.loads(aprobar.content) == {"name": "aviso_cita_v1", "category": "UTILITY"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "category"), [("Aviso Cita", "UTILITY"), ("aviso", "SERVICE")])
async def test_aprobacion_invalida_no_sale_a_red(name, category):
    client, seen = _client(lambda r: httpx.Response(201, json={}))
    with pytest.raises(ChannelError):
        await content.request_whatsapp_approval(client, HX, name=name, category=category)
    assert seen == []


@pytest.mark.asyncio
async def test_estado_de_aprobacion_normalizado():
    client, _ = _client(
        lambda r: httpx.Response(
            200,
            json={
                "whatsapp": {
                    "status": "Rejected",
                    "name": "x",
                    "category": "MARKETING",
                    "rejection_reason": "duplicada",
                }
            },
        )
    )
    approval = await content.fetch_approval(client, HX)
    assert (approval.status, approval.rejection_reason, approval.approved) == (
        "rejected",
        "duplicada",
        False,
    )
    client, _ = _client(lambda r: httpx.Response(200, json={}))
    assert (await content.fetch_approval(client, HX)).status == "unsubmitted"


@pytest.mark.asyncio
async def test_borrar_devuelve_vacio_y_valida_el_sid():
    client, seen = _client(lambda r: httpx.Response(204))
    await content.delete_content(client, HX)
    assert seen[0].method == "DELETE"
    with pytest.raises(ChannelError):
        await content.delete_content(client, "HX1")

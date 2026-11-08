"""Paquete `channel-twilio` (ADR-0186, PR-2 de WhatsApp vía Twilio).

Tests puros, sin red: el cliente HTTP usa `httpx.MockTransport`. La firma y la tabla de errores
tienen sus propios casos más abajo; el webhook, las tablas y el alta llegan en los PR siguientes.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx
import pytest
from agentcore_channel_twilio import PROVIDER, InboundAdapter, OutboundAdapter
from agentcore_channel_twilio.adapters import TwilioInbound, TwilioOutbound, whatsapp_address
from agentcore_channel_twilio.client import TwilioClient, require_sid
from agentcore_channel_twilio.content import (
    Approval,
    ContentTemplate,
    create_content,
    fetch_approval,
    list_contents_with_approvals,
    placeholders,
    submit_whatsapp_approval,
    validate_variables,
)
from agentcore_channel_twilio.errors import CODE_CATEGORIES, category_for_code, error_from_response
from agentcore_channel_twilio.senders import get_sender, list_whatsapp_senders, set_webhooks
from agentcore_channel_twilio.signature import compute_signature, validate_signature
from agentcore_channel_twilio.status import parse_status

from core.gateway.channels.base import ProviderError

AC = "AC" + "a" * 32
XE = "XE" + "b" * 32
HX = "HX" + "c" * 32
MG = "MG" + "d" * 32
TOKEN = "token-de-prueba"
CREDS = {"account_sid": AC, "auth_token": TOKEN, "from": "whatsapp:+15550001111"}


def _transport(handler):
    calls: list[httpx.Request] = []

    def _wrap(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(_wrap), calls


def _form(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(request.content.decode(), keep_blank_values=True)


# --- módulo y registro -----------------------------------------------------------------------


def test_el_modulo_expone_lo_que_lee_el_registry():
    assert PROVIDER == "twilio"
    assert InboundAdapter is TwilioInbound
    assert OutboundAdapter is TwilioOutbound
    # El registry instancia sin argumentos.
    assert TwilioOutbound().provider == "twilio"
    assert TwilioInbound().channel == "whatsapp"


@pytest.mark.parametrize("prefix", ["AC", "XE", "HX", "MG"])
def test_require_sid_rechaza_lo_que_alteraria_la_ruta(prefix):
    ok = prefix + "0" * 32
    assert require_sid(ok, prefix) == ok
    for malo in [
        prefix + "0" * 31,
        prefix + "0" * 32 + "/../x",
        "",
        prefix.lower() + "0" * 32,
        None,
    ]:
        with pytest.raises(ProviderError) as exc:
            require_sid(malo, prefix)  # type: ignore[arg-type]
        assert exc.value.category == "CONFIG"


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("whatsapp:+573001234567", "whatsapp:+573001234567"),
        ("+573001234567", "whatsapp:+573001234567"),
        ("573001234567", "whatsapp:+573001234567"),
    ],
)
def test_whatsapp_address_normaliza(valor, esperado):
    assert whatsapp_address(valor) == esperado


@pytest.mark.parametrize("malo", ["", "whatsapp:", "hola", "+0123", "+57300abc"])
def test_whatsapp_address_rechaza(malo):
    with pytest.raises(ProviderError) as exc:
        whatsapp_address(malo)
    assert exc.value.category == "RECIPIENT"


# --- entrada ---------------------------------------------------------------------------------


def _entrante(**extra):
    base = {
        "MessageSid": "SM" + "1" * 32,
        "AccountSid": AC,
        "From": "whatsapp:+573001234567",
        "To": "whatsapp:+15550001111",
        "Body": "Hola",
        "ProfileName": "Ana",
        "NumMedia": "0",
    }
    return {**base, **extra}


def test_parse_inbound_texto():
    [msg] = TwilioInbound().parse_inbound(_entrante())
    assert msg.provider == "twilio"
    assert msg.channel == "whatsapp"
    assert msg.external_message_id == "SM" + "1" * 32
    # La identidad conserva el prefijo; la clave de ruteo es el E.164 del sender.
    assert msg.customer_external_id == "whatsapp:+573001234567"
    assert msg.connection_external_id == "+15550001111"
    assert msg.customer_name == "Ana"
    assert msg.content == "Hola"
    assert msg.media == []
    assert msg.interactive_id == ""


def test_parse_inbound_media():
    payload = _entrante(
        Body="",
        NumMedia="2",
        MediaUrl0="https://api.twilio.com/2010-04-01/Accounts/x/Messages/y/Media/z0",
        MediaContentType0="image/jpeg",
        MediaUrl1="https://api.twilio.com/2010-04-01/Accounts/x/Messages/y/Media/z1",
        MediaContentType1="audio/ogg",
    )
    [msg] = TwilioInbound().parse_inbound(payload)
    assert [m["type"] for m in msg.media] == ["image/jpeg", "audio/ogg"]
    assert msg.media[0]["id"] == f"{payload['MessageSid']}:0"
    assert msg.media[1]["url"].endswith("/z1")


def test_parse_inbound_tope_de_media():
    payload = _entrante(NumMedia="999", **{f"MediaUrl{n}": f"https://x/{n}" for n in range(20)})
    [msg] = TwilioInbound().parse_inbound(payload)
    assert len(msg.media) == 10


@pytest.mark.parametrize(
    ("extra", "contenido", "interactivo"),
    [
        (
            {"Body": "Sí", "ButtonText": "Sí", "ButtonPayload": "confirmar:abc"},
            "Sí",
            "confirmar:abc",
        ),
        ({"Body": "", "ListId": "plan:premium", "ListTitle": "Premium"}, "Premium", "plan:premium"),
    ],
)
def test_parse_inbound_boton_y_lista(extra, contenido, interactivo):
    [msg] = TwilioInbound().parse_inbound(_entrante(**extra))
    assert msg.content == contenido
    assert msg.interactive_id == interactivo


@pytest.mark.parametrize(
    "payload",
    [
        {},
        _entrante(MessageSid=""),
        _entrante(From="whatsapp:basura"),
        # Ubicación sola: el core no la procesa, igual que con Meta.
        _entrante(Body="", Latitude="4.6", Longitude="-74.0"),
    ],
)
def test_parse_inbound_descarta(payload):
    assert TwilioInbound().parse_inbound(payload) == []


def test_verify_request_falla_cerrado_sin_url_ni_secreto():
    inbound = TwilioInbound()
    assert inbound.verify_request(b"a=1", {"X-Twilio-Signature": "x"}, TOKEN) is False
    assert (
        inbound.verify_request(b"a=1", {"X-Twilio-Signature": "x"}, "", url="https://x/y") is False
    )


# --- estados ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("twilio", "core"),
    [
        ("accepted", "queued"),
        ("queued", "queued"),
        ("sending", "queued"),
        ("sent", "sent"),
        ("delivered", "delivered"),
        ("read", "read"),
        ("undelivered", "failed"),
        ("failed", "failed"),
        ("canceled", "failed"),
    ],
)
def test_parse_status_mapea_al_core(twilio, core):
    update = parse_status({"MessageSid": "SM1", "MessageStatus": twilio})
    assert update is not None
    assert update["id"] == "SM1"
    assert update["status"] == core


def test_parse_status_con_error():
    update = parse_status(
        {
            "MessageSid": "SM1",
            "MessageStatus": "undelivered",
            "ErrorCode": "63016",
            "ErrorMessage": "fuera",
        }
    )
    assert update["errors"] == [{"code": 63016, "title": "fuera"}]


@pytest.mark.parametrize(
    "payload", [{}, {"MessageSid": "SM1"}, {"MessageSid": "SM1", "MessageStatus": "nuevo"}]
)
def test_parse_status_ignora_lo_desconocido(payload):
    assert parse_status(payload) is None


# --- salida ----------------------------------------------------------------------------------


def _ok_mensaje(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"sid": "SM" + "9" * 32, "status": "queued"})


async def test_send_text_arma_el_formulario():
    transport, calls = _transport(_ok_mensaje)
    creds = {
        **CREDS,
        "status_callback_url": "https://test.solvershub.site/webhooks/twilio/pub/status",
    }
    sid = await TwilioOutbound(transport).send_text(creds, "whatsapp:+573001234567", "Hola")
    assert sid == "SM" + "9" * 32
    [req] = calls
    assert req.method == "POST"
    assert str(req.url) == f"https://api.twilio.com/2010-04-01/Accounts/{AC}/Messages.json"
    assert req.headers["authorization"].startswith("Basic ")
    assert _form(req) == [
        ("To", "whatsapp:+573001234567"),
        ("From", "whatsapp:+15550001111"),
        ("Body", "Hola"),
        ("StatusCallback", "https://test.solvershub.site/webhooks/twilio/pub/status"),
    ]


async def test_send_con_messaging_service():
    transport, calls = _transport(_ok_mensaje)
    creds = {"account_sid": AC, "auth_token": TOKEN, "messaging_service_sid": MG}
    await TwilioOutbound(transport).send_text(creds, "+573001234567", "Hola")
    assert ("MessagingServiceSid", MG) in _form(calls[0])
    assert all(k != "From" for k, _ in _form(calls[0]))


async def test_send_image_y_plantilla():
    transport, calls = _transport(_ok_mensaje)
    out = TwilioOutbound(transport)
    await out.send_image(CREDS, "+573001234567", "https://cdn.example.com/a.jpg", caption="Mira")
    await out.send_content_template(CREDS, "+573001234567", HX, {"1": "Ana", "2": "lunes"})
    imagen, plantilla = (_form(r) for r in calls)
    assert ("MediaUrl", "https://cdn.example.com/a.jpg") in imagen and ("Body", "Mira") in imagen
    assert ("ContentSid", HX) in plantilla
    assert json.loads(dict(plantilla)["ContentVariables"]) == {"1": "Ana", "2": "lunes"}
    assert "Body" not in dict(plantilla)


@pytest.mark.parametrize(
    "creds",
    [
        {"account_sid": AC, "auth_token": TOKEN},
        {"account_sid": AC, "auth_token": TOKEN, "from": "whatsapp:basura"},
        {"account_sid": "ACmal", "auth_token": TOKEN, "from": "+15550001111"},
        {"account_sid": AC, "auth_token": "", "from": "+15550001111"},
    ],
)
async def test_send_sin_configuracion_es_config_y_no_sale_a_red(creds):
    transport, calls = _transport(_ok_mensaje)
    with pytest.raises(ProviderError) as exc:
        await TwilioOutbound(transport).send_text(creds, "+573001234567", "Hola")
    assert exc.value.category == "CONFIG"
    assert calls == []


async def test_send_a_numero_invalido_es_recipient():
    transport, calls = _transport(_ok_mensaje)
    with pytest.raises(ProviderError) as exc:
        await TwilioOutbound(transport).send_text(CREDS, "whatsapp:nadie", "Hola")
    assert exc.value.category == "RECIPIENT"
    assert calls == []


async def test_send_sin_sid_es_ambiguo():
    transport, _ = _transport(lambda r: httpx.Response(201, json={}))
    with pytest.raises(ProviderError) as exc:
        await TwilioOutbound(transport).send_text(CREDS, "+573001234567", "Hola")
    assert exc.value.category == "AMBIGUOUS"


# --- cliente ---------------------------------------------------------------------------------


def _lanza(exc_type):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("boom", request=request)

    return handler


@pytest.mark.parametrize(
    ("exc_type", "post", "get"),
    [
        (httpx.ConnectError, "TRANSIENT", "TRANSIENT"),
        (httpx.ConnectTimeout, "TRANSIENT", "TRANSIENT"),
        (httpx.ReadTimeout, "AMBIGUOUS", "TRANSIENT"),
        (httpx.RemoteProtocolError, "AMBIGUOUS", "TRANSIENT"),
    ],
)
async def test_fallas_de_red_por_metodo(exc_type, post, get):
    transport, _ = _transport(_lanza(exc_type))
    client = TwilioClient(AC, TOKEN, transport=transport)
    with pytest.raises(ProviderError) as e_post:
        await client.post_form(f"{client.account_url}/Messages.json", [("Body", "x")], op="t")
    with pytest.raises(ProviderError) as e_get:
        await client.get(f"{client.account_url}.json", op="t")
    assert e_post.value.category == post
    assert e_get.value.category == get


async def test_cliente_no_sale_de_los_hosts_de_twilio():
    transport, calls = _transport(_ok_mensaje)
    client = TwilioClient(AC, TOKEN, transport=transport)
    for url in [
        "https://evil.example.com/x",
        "http://api.twilio.com/x",
        "https://api.twilio.com.evil.com/x",
    ]:
        with pytest.raises(ProviderError) as exc:
            await client.get(url, op="t")
        assert exc.value.category == "CONFIG"
    assert calls == []


async def test_paginacion_no_sigue_a_otro_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"senders": [], "meta": {"next_page_url": "https://evil.example.com/p2"}}
        )

    transport, calls = _transport(handler)
    client = TwilioClient(AC, TOKEN, transport=transport)
    with pytest.raises(ProviderError):
        await client.paginate(
            "https://messaging.twilio.com/v2/Channels/Senders", key="senders", op="t"
        )
    assert len(calls) == 1


# --- senders ---------------------------------------------------------------------------------


def _sender_json(sid=XE, status="ONLINE", sender_id="whatsapp:+15550001111", **extra):
    return {
        "sid": sid,
        "status": status,
        "sender_id": sender_id,
        "profile": {"name": "SolversHub"},
        "webhook": {"callback_url": "https://otra.integracion/in"},
        "properties": {"quality_rating": "HIGH"},
        **extra,
    }


async def test_lista_solo_senders_de_whatsapp_y_pagina():
    pagina2 = "https://messaging.twilio.com/v2/Channels/Senders?Channel=whatsapp&PageSize=50&Page=1"

    def handler(request: httpx.Request) -> httpx.Response:
        if "Page=1" in str(request.url):
            return httpx.Response(
                200,
                json={"senders": [_sender_json(sid="XE" + "e" * 32, status="OFFLINE")], "meta": {}},
            )
        return httpx.Response(
            200,
            json={
                "senders": [_sender_json(), _sender_json(sender_id="+15550002222")],
                "meta": {"next_page_url": pagina2},
            },
        )

    transport, calls = _transport(handler)
    senders = await list_whatsapp_senders(TwilioClient(AC, TOKEN, transport=transport))
    assert [s.sid for s in senders] == [XE, "XE" + "e" * 32]
    assert senders[0].online and not senders[1].online
    assert senders[0].e164 == "+15550001111"
    assert senders[0].display_name == "SolversHub"
    assert senders[0].webhook["callback_url"] == "https://otra.integracion/in"
    assert calls[0].url.params["Channel"] == "whatsapp"


async def test_set_webhooks_por_post_json():
    transport, calls = _transport(lambda r: httpx.Response(200, json=_sender_json()))
    client = TwilioClient(AC, TOKEN, transport=transport)
    await set_webhooks(client, XE, callback_url="https://t/in", status_callback_url="https://t/st")
    [req] = calls
    assert str(req.url) == f"https://messaging.twilio.com/v2/Channels/Senders/{XE}"
    assert json.loads(req.content) == {
        "webhook": {
            "callback_url": "https://t/in",
            "callback_method": "POST",
            "status_callback_url": "https://t/st",
            "status_callback_method": "POST",
        }
    }
    with pytest.raises(ProviderError):
        await get_sender(client, "XE/../../Accounts")


# --- plantillas ------------------------------------------------------------------------------


def test_placeholders():
    assert placeholders(["Hola {{1}}, tu cita es el {{ 2 }}", "{{1}}"]) == [1, 2]
    assert placeholders(["Sin variables"]) == []


@pytest.mark.parametrize(
    ("textos", "ejemplos"),
    [
        (["Hola {{1}} y {{3}}"], {"1": "a", "3": "c"}),
        (["Hola {{1}}"], {}),
        (["Hola {{1}}"], {"1": "a", "2": "b"}),
        (["Hola {{1}}"], {"1": "  "}),
    ],
)
def test_validate_variables_rechaza(textos, ejemplos):
    with pytest.raises(ProviderError) as exc:
        validate_variables(textos, ejemplos)
    assert exc.value.category == "CONTENT"


def test_validate_variables_acepta():
    validate_variables(["Hola {{1}}, tu cita es el {{2}}"], {"1": "Ana", "2": "lunes"})
    validate_variables(["Sin variables"], {})


async def test_crear_enviar_y_consultar_aprobacion():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/Content":
            body = json.loads(request.content)
            return httpx.Response(201, json={"sid": HX, **body})
        if path.endswith("/ApprovalRequests/whatsapp"):
            return httpx.Response(201, json={**json.loads(request.content), "status": "received"})
        if path.endswith("/ApprovalRequests"):
            return httpx.Response(
                200,
                json={
                    "whatsapp": {
                        "status": "rejected",
                        "rejection_reason": "INVALID_FORMAT",
                        "category": "utility",
                    }
                },
            )
        return httpx.Response(404, json={"code": 20404, "message": "no"})

    transport, calls = _transport(handler)
    client = TwilioClient(AC, TOKEN, transport=transport)
    plantilla = await create_content(
        client,
        friendly_name="recordatorio_cita",
        language="es",
        types={"twilio/text": {"body": "Hola {{1}}"}},
        variables={"1": "Ana"},
    )
    assert plantilla.sid == HX
    assert plantilla.approval.status == "unsubmitted"
    enviada = await submit_whatsapp_approval(
        client, HX, name="recordatorio_cita", category="utility"
    )
    assert enviada == Approval(status="received", name="recordatorio_cita", category="UTILITY")
    assert json.loads(calls[1].content) == {"name": "recordatorio_cita", "category": "UTILITY"}
    revisada = await fetch_approval(client, HX)
    assert (revisada.status, revisada.rejection_reason, revisada.category) == (
        "rejected",
        "INVALID_FORMAT",
        "UTILITY",
    )
    with pytest.raises(ProviderError) as exc:
        await submit_whatsapp_approval(client, HX, name="x", category="PROMO")
    assert exc.value.category == "CONTENT"


async def test_lista_contenidos_con_aprobacion():
    filas = [
        {
            "sid": HX,
            "friendly_name": "a",
            "language": "es",
            "approval_requests": {"status": "approved", "name": "a"},
        },
        {"sid": "HX" + "f" * 32, "friendly_name": "b", "language": "es", "approval_requests": {}},
    ]
    transport, _ = _transport(lambda r: httpx.Response(200, json={"contents": filas, "meta": {}}))
    todas = await list_contents_with_approvals(TwilioClient(AC, TOKEN, transport=transport))
    assert [p.approval.status for p in todas] == ["approved", "unsubmitted"]
    assert isinstance(todas[0], ContentTemplate)


# --- firma (vectores de la documentación de Twilio y de twilio-python) ------------------------


_URI = "https://mycompany.com/myapp.php?foo=1&bar=2"
_PARAMS = [
    ("CallSid", "CA1234567890ABCDE"),
    ("Digits", "1234"),
    ("From", "+14158675309"),
    ("To", "+18005551212"),
    ("Caller", "+14158675309"),
]


def test_firma_vector_de_la_documentacion():
    params = [
        ("Digits", "1234"),
        ("To", "+18005551212"),
        ("From", "+14158675310"),
        ("Caller", "+14158675310"),
        ("CallSid", "CA1234567890ABCDE"),
    ]
    firma = compute_signature("12345", "https://example.com/myapp.php?foo=1&bar=2", params)
    assert firma == "L/OH5YylLD5NRKLltdqwSvS0BnU="


def test_firma_vectores_de_twilio_python():
    assert compute_signature("12345", _URI, _PARAMS) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    repetidos = [
        ("Sid", "CA123"),
        ("SidAccount", "AC123"),
        ("Digits", "5678"),
        ("Digits", "1234"),
        ("Digits", "1234"),
    ]
    assert compute_signature("12345", _URI, repetidos) == "IK+Dwps556ElfBT0I3Rgjkr1wJU="
    assert compute_signature(
        "12345", "http://mycompany.com:1234/myapp.php?foo=1&bar=2", _PARAMS
    ) == ("Zmvh+3yNM1Phv2jhDCwEM3q5ebU=")


def test_firma_acepta_con_y_sin_puerto():
    # Firmada con :443 explícito, validada contra la URL sin puerto, y al revés.
    assert validate_signature("12345", _URI, _PARAMS, "kvajT1Ptam85bY51eRf/AJRuM3w=")
    assert validate_signature(
        "12345",
        "http://mycompany.com/myapp.php?foo=1&bar=2",
        _PARAMS,
        "0ZXoZLH/DfblKGATFgpif+LLRf4=",
    )
    assert validate_signature(
        "12345",
        "https://mycompany.com:1234/myapp.php?foo=1&bar=2",
        _PARAMS,
        "RSOYDt4T1cUTdK1PDd93/VVr8B8=",
    )


@pytest.mark.parametrize(
    ("token", "url", "params", "firma"),
    [
        ("12346", _URI, _PARAMS, "RSOYDt4T1cUTdK1PDd93/VVr8B8="),  # otro token
        ("12345", _URI + "&x=1", _PARAMS, "RSOYDt4T1cUTdK1PDd93/VVr8B8="),  # otra URL
        (
            "12345",
            _URI,
            [*_PARAMS, ("Body", "hola")],
            "RSOYDt4T1cUTdK1PDd93/VVr8B8=",
        ),  # parámetro agregado
        ("12345", _URI, _PARAMS, ""),
        ("", _URI, _PARAMS, "RSOYDt4T1cUTdK1PDd93/VVr8B8="),
    ],
)
def test_firma_rechaza(token, url, params, firma):
    assert validate_signature(token, url, params, firma) is False


def test_verify_request_con_el_formulario_crudo():
    url = "https://test.solvershub.site/webhooks/twilio/pub123/inbound"
    params = [
        ("MessageSid", "SM1"),
        ("Body", "Hola, ¿cómo vas? 100% & más"),
        ("From", "whatsapp:+573001234567"),
    ]
    raw = str(httpx.QueryParams(params)).encode()
    firma = compute_signature(TOKEN, url, params)
    inbound = TwilioInbound()
    assert inbound.verify_request(raw, {"x-twilio-signature": firma}, TOKEN, url=url)
    assert inbound.verify_request(raw, {"X-Twilio-Signature": firma}, TOKEN, url=url)
    # La firma va contra la URL registrada: otra URL (la que vería el proxy) no valida.
    assert not inbound.verify_request(
        raw,
        {"x-twilio-signature": firma},
        TOKEN,
        url="http://backend:8000/webhooks/twilio/pub123/inbound",
    )


# --- errores ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "categoria"),
    [
        (20003, "AUTH"),
        (20429, "RATE_LIMIT"),
        (63018, "RATE_LIMIT"),
        (63038, "QUOTA"),
        (63016, "WINDOW"),
        (21610, "OPT_OUT"),
        (21211, "RECIPIENT"),
        (63003, "RECIPIENT"),
        (63024, "RECIPIENT"),
        (63007, "CONFIG"),
        (63112, "CONFIG"),
        (63005, "CONTENT"),
        (63049, "PERMANENT"),
    ],
)
def test_tabla_de_codigos(code, categoria):
    assert category_for_code(code) == categoria


def test_codigo_desconocido_cae_por_estado_http():
    assert category_for_code(99999, 401) == "AUTH"
    assert category_for_code(None, 403) == "AUTH"
    assert category_for_code(None, 429) == "RATE_LIMIT"
    assert category_for_code(99999, 400) == "PERMANENT"
    assert category_for_code(None, 503) == "TRANSIENT"


def test_toda_categoria_de_la_tabla_existe_en_el_core():
    from typing import get_args

    from core.gateway.channels.base import ErrorCategory

    assert set(CODE_CATEGORIES.values()) <= set(get_args(ErrorCategory))


def test_error_from_response():
    resp = httpx.Response(
        400,
        json={
            "code": 21610,
            "message": "Attempt to send to unsubscribed recipient " + "x" * 500,
            "more_info": "https://www.twilio.com/docs/errors/21610",
            "status": 400,
        },
    )
    err = error_from_response(resp)
    assert (err.provider, err.code, err.http_status, err.category) == (
        "twilio",
        21610,
        400,
        "OPT_OUT",
    )
    assert len(str(err)) < 340
    sin_cuerpo = error_from_response(httpx.Response(503, text="<html>caído</html>"))
    assert (sin_cuerpo.code, sin_cuerpo.category) == (None, "TRANSIENT")
    con_espera = error_from_response(
        httpx.Response(429, headers={"Retry-After": "7"}, json={"code": 20429})
    )
    assert (con_espera.category, con_espera.retry_after) == ("RATE_LIMIT", 7.0)


async def test_el_cliente_clasifica_el_error_http():
    transport, _ = _transport(
        lambda r: httpx.Response(400, json={"code": 63016, "message": "fuera de ventana"})
    )
    with pytest.raises(ProviderError) as exc:
        await TwilioOutbound(transport).send_text(CREDS, "+573001234567", "Hola")
    assert exc.value.category == "WINDOW"


async def test_decide_retry_con_errores_de_twilio():
    from core.tasks.outbound_send import decide_retry

    assert (
        decide_retry(error_from_response(httpx.Response(400, json={"code": 63016})), 0).action
        == "fail_permanent"
    )
    assert (
        decide_retry(error_from_response(httpx.Response(400, json={"code": 21610})), 0).action
        == "fail_permanent"
    )
    assert (
        decide_retry(error_from_response(httpx.Response(429, json={"code": 20429})), 0).action
        == "retry"
    )
    assert decide_retry(error_from_response(httpx.Response(503)), 0).action == "retry"


def test_la_tabla_entera_es_deliberada():
    """Cambiar la categoría de un código (o agregar uno) obliga a tocar esta copia: así se revisa a propósito."""
    assert CODE_CATEGORIES == {
        20003: "AUTH",
        30002: "AUTH",
        30001: "TRANSIENT",
        30008: "TRANSIENT",
        20429: "RATE_LIMIT",
        63018: "RATE_LIMIT",
        63038: "QUOTA",
        63016: "WINDOW",
        21610: "OPT_OUT",
        21211: "RECIPIENT",
        21612: "RECIPIENT",
        21614: "RECIPIENT",
        63003: "RECIPIENT",
        63024: "RECIPIENT",
        63032: "RECIPIENT",
        30003: "RECIPIENT",
        30005: "RECIPIENT",
        30006: "RECIPIENT",
        21606: "CONFIG",
        21608: "CONFIG",
        21408: "CONFIG",
        63007: "CONFIG",
        63051: "CONFIG",
        63112: "CONFIG",
        63020: "CONFIG",
        63005: "CONTENT",
        63013: "CONTENT",
        63021: "CONTENT",
        63027: "CONTENT",
        63028: "CONTENT",
        63030: "CONTENT",
        30007: "CONTENT",
        63049: "PERMANENT",
        30004: "PERMANENT",
        20404: "PERMANENT",
    }


@pytest.mark.parametrize(("metodo", "esperada"), [("post", "AMBIGUOUS"), ("get", "TRANSIENT")])
async def test_2xx_sin_json_queda_clasificado(metodo, esperada):
    transport, _ = _transport(lambda r: httpx.Response(200, text="<html>ok</html>"))
    client = TwilioClient(AC, TOKEN, transport=transport)
    with pytest.raises(ProviderError) as exc:
        if metodo == "post":
            await client.post_form(f"{client.account_url}/Messages.json", [("Body", "x")], op="t")
        else:
            await client.get(f"{client.account_url}.json", op="t")
    assert exc.value.category == esperada

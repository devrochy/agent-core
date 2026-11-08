"""Firma `X-Twilio-Signature` (ADR-0186). El vector es el ejemplo de
https://www.twilio.com/docs/usage/security#validating-requests (leído el 2026-11-07)."""

from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from agentcore_channel_twilio.adapters import TwilioInbound
from agentcore_channel_twilio.signature import compute_signature, is_valid

URL = "https://example.com/myapp.php?foo=1&bar=2"
TOKEN = "12345"
PARAMS = [
    ("CallSid", "CA1234567890ABCDE"),
    ("Caller", "+14158675310"),
    ("Digits", "1234"),
    ("From", "+14158675310"),
    ("To", "+18005551212"),
]
EXPECTED = "L/OH5YylLD5NRKLltdqwSvS0BnU="


def test_vector_de_la_documentacion():
    assert compute_signature(URL, PARAMS, TOKEN) == EXPECTED


def test_el_orden_de_llegada_no_cambia_la_firma():
    assert compute_signature(URL, list(reversed(PARAMS)), TOKEN) == EXPECTED


def test_cuerpo_form_urlencoded_valido():
    body = urlencode(PARAMS).encode()  # el `+` llega como %2B
    assert is_valid(URL, body, EXPECTED, TOKEN, content_type="application/x-www-form-urlencoded")


def test_token_url_o_cuerpo_distinto_no_valida():
    body = urlencode(PARAMS).encode()
    assert not is_valid(URL, body, EXPECTED, "otro-token")
    assert not is_valid("https://example.com/otra", body, EXPECTED, TOKEN)
    assert not is_valid(URL, urlencode([*PARAMS, ("Body", "x")]).encode(), EXPECTED, TOKEN)
    assert not is_valid(URL, body, "", TOKEN)
    assert not is_valid("", body, EXPECTED, TOKEN)


def test_puerto_por_defecto_en_la_url_guardada_o_en_la_firmada():
    body = urlencode(PARAMS).encode()
    con_puerto = "https://example.com:443/myapp.php?foo=1&bar=2"
    firma_con_puerto = compute_signature(con_puerto, PARAMS, TOKEN)
    assert is_valid(URL, body, firma_con_puerto, TOKEN)
    assert is_valid(con_puerto, body, EXPECTED, TOKEN)
    assert not is_valid("https://example.com:8443/myapp.php?foo=1&bar=2", body, EXPECTED, TOKEN)


def test_claves_repetidas_y_unicode():
    params = [("Body", "¿Cuánto vale? 🙂"), ("MediaUrl", "b"), ("MediaUrl", "a")]
    firma = compute_signature(URL, params, TOKEN)
    assert firma == compute_signature(URL, [params[0], params[2], params[1]], TOKEN)
    assert is_valid(URL, urlencode(params).encode(), firma, TOKEN)


def test_cuerpo_json_firma_la_url_con_body_sha256():
    raw = b'{"MessageSid":"SM1"}'
    url = f"https://example.com/hook?bodySHA256={hashlib.sha256(raw).hexdigest()}"
    firma = compute_signature(url, [], TOKEN)
    assert is_valid(url, raw, firma, TOKEN, content_type="application/json")
    assert not is_valid(url, b'{"MessageSid":"SM2"}', firma, TOKEN, content_type="application/json")
    assert not is_valid(
        "https://example.com/hook",
        raw,
        compute_signature("https://example.com/hook", [], TOKEN),
        TOKEN,
        content_type="application/json",
    )


def test_el_adaptador_exige_la_url_guardada():
    body = urlencode(PARAMS).encode()
    headers = {"X-Twilio-Signature": EXPECTED, "Content-Type": "application/x-www-form-urlencoded"}
    adapter = TwilioInbound()
    assert adapter.verify_request(body, headers, TOKEN, url=URL)
    assert not adapter.verify_request(body, headers, TOKEN)  # sin URL, nunca
    assert not adapter.verify_request(
        body, headers, TOKEN, url="https://proxy.interno/myapp.php?foo=1&bar=2"
    )

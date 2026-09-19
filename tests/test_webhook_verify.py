"""HMAC + handshake de webhooks de Meta (core/gateway/webhook_verify.py)."""

from __future__ import annotations

import hashlib
import hmac
import json

from core.gateway.webhook_verify import constant_time_equal, verify_signature

SECRET = "app-secret-de-prueba"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_firma_valida():
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    assert verify_signature(body, _sign(body), SECRET) is True


def test_firma_alterada_falla():
    body = json.dumps({"a": 1}).encode()
    other_body = json.dumps({"a": 2}).encode()
    assert verify_signature(body, _sign(other_body), SECRET) is False


def test_firma_sin_prefijo_sha256_falla():
    body = b"{}"
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, expected, SECRET) is False  # sin "sha256="


def test_fallback_unicode_escapado():
    """Meta a veces firma sobre una variante con unicode escapado
    (ensure_ascii=True) en vez de los bytes crudos con caracteres UTF-8."""
    payload = {"name": "José"}
    raw_body = json.dumps(payload, ensure_ascii=False).encode()  # bytes "crudos" (con é)
    escaped_body = json.dumps(payload, ensure_ascii=True).encode()  # variante escapada

    # la firma se calculó sobre la variante ESCAPADA, no sobre los bytes crudos
    signature = _sign(escaped_body)
    assert verify_signature(raw_body, signature, SECRET) is True


def test_fallback_no_rescata_una_firma_realmente_invalida():
    body = json.dumps({"x": 1}).encode()
    assert verify_signature(body, "sha256=" + "0" * 64, SECRET) is False


def test_body_no_json_sin_prefijo_correcto_no_revienta():
    assert verify_signature(b"no es json ni tiene forma", "sha256=abc", SECRET) is False


def test_constant_time_equal():
    assert constant_time_equal("tok-123", "tok-123") is True
    assert constant_time_equal("tok-123", "tok-456") is False
    assert constant_time_equal("corto", "un-string-mucho-mas-largo") is False

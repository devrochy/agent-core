"""Proveedor Wompi de la suscripción de plataforma (ADR-0046).

Unitario (sin red): firma, parseo del webhook, mínimo de monto. La creación real
del link se prueba con httpx mockeado.
"""

from __future__ import annotations

import hashlib

import pytest

from core.billing.providers.wompi import WompiBillingProvider


def _provider() -> WompiBillingProvider:
    return WompiBillingProvider(
        public_key="pub_test", private_key="prv_test", events_secret="events_secret", sandbox=True
    )


def _signed_payload(properties: list[str], data: dict, timestamp: int, secret: str) -> dict:
    parts = []
    for path in properties:
        value: object = data
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
            if value is None:
                break
        parts.append(str(value) if value is not None else "")
    checksum = hashlib.sha256(("".join(parts) + str(timestamp) + secret).encode()).hexdigest()
    return {
        "signature": {"properties": properties, "checksum": checksum},
        "timestamp": timestamp,
        "data": data,
    }


def test_verify_webhook_signature_valida():
    provider = _provider()
    payload = _signed_payload(
        ["transaction.status"],
        {"transaction": {"status": "APPROVED", "payment_link_id": "link-1"}},
        timestamp=1700000000,
        secret="events_secret",
    )
    assert provider.verify_webhook_signature(payload) is True


def test_verify_webhook_signature_rechaza_secret_incorrecto():
    provider = _provider()
    payload = _signed_payload(
        ["transaction.status"],
        {"transaction": {"status": "APPROVED"}},
        timestamp=1700000000,
        secret="otro-secret",
    )
    assert provider.verify_webhook_signature(payload) is False


def test_verify_webhook_signature_sin_datos_es_false():
    provider = _provider()
    assert provider.verify_webhook_signature({}) is False
    assert provider.verify_webhook_signature({"signature": {}, "timestamp": 1}) is False


def test_parse_webhook_usa_payment_link_id_no_id():
    """Correlaciona por `payment_link_id` (lo guardado), no por `id` (cambia por intento)."""
    provider = _provider()
    payload = {
        "data": {
            "transaction": {
                "id": "txn-intento-1",
                "payment_link_id": "link-real",
                "status": "APPROVED",
                "amount_in_cents": 20000000,
            }
        }
    }
    event = provider.parse_webhook_event(payload)
    assert event.provider_reference == "link-real"
    assert event.status == "aprobado"
    assert event.amount_in_cents == 20000000


def test_parse_webhook_normaliza_estados():
    provider = _provider()
    for raw, expected in [
        ("DECLINED", "rechazado"),
        ("VOIDED", "rechazado"),
        ("ERROR", "rechazado"),
        ("PENDING", "pendiente"),
        ("DESCONOCIDO", "pendiente"),
    ]:
        payload = {"data": {"transaction": {"payment_link_id": "l", "status": raw}}}
        assert provider.parse_webhook_event(payload).status == expected


@pytest.mark.asyncio
async def test_create_payment_link_rechaza_bajo_minimo():
    from core.billing.providers.base import BillingRequestRejected

    provider = _provider()
    with pytest.raises(BillingRequestRejected):
        await provider.create_payment_link(reference="r", amount_cop=100_000)


@pytest.mark.asyncio
async def test_create_payment_link_ok(monkeypatch):
    provider = _provider()

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"id": "link-abc"}}

    captured: dict = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr("core.billing.providers.wompi.httpx.AsyncClient", _Client)

    link = await provider.create_payment_link(reference="r", amount_cop=200_000)
    assert link.provider_reference == "link-abc"
    assert link.url == "https://checkout.wompi.co/l/link-abc"
    assert captured["json"]["collect_shipping"] is False
    assert captured["json"]["amount_in_cents"] == 20_000_000
    assert captured["json"]["currency"] == "COP"


@pytest.mark.asyncio
async def test_link_visible_al_cliente_sin_marca_tecnica(monkeypatch):
    """HU-REP-04: el nombre/descripción del link usan el plan y SolversHub."""
    provider = _provider()

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"id": "link-abc"}}

    captured: dict = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr("core.billing.providers.wompi.httpx.AsyncClient", _Client)

    await provider.create_payment_link(reference="r", amount_cop=200_000, plan_name="Premium")
    body = captured["json"]
    blob = f"{body['name']} {body['description']}"
    assert "agent-core" not in blob.lower()
    assert "SolversHub" in blob
    assert "Premium" in blob

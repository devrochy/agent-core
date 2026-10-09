"""WompiProvider: verificación de firma de webhook, parseo de eventos, y el
body/comportamiento real de `create_payment_link` -- verificado contra el
sandbox REAL de Wompi (tenant yopoldo, 2026-09-11: un 422 real por
`collect_shipping` faltante) y contra la implementación ya probada de
agent-sale (`src/payments/wompiClient.ts`, `wompiWebhookHandler.ts`).

La verificación de firma es la parte de seguridad crítica de pack-payments
(un webhook sin verificar dejaría marcar pedidos como pagados a cualquiera
que le pegue a la URL) -- se prueba con un checksum calculado a mano con el
mismo algoritmo documentado por Wompi (docs.wompi.co/docs/colombia/eventos):
concatenar los valores de `signature.properties` (en orden, sin separador)
+ `timestamp` + `events_secret`, SHA256, comparar contra `signature.checksum`.
"""

from __future__ import annotations

import hashlib

import pytest
from pack_payments.providers.base import PaymentRequestRejected
from pack_payments.providers.wompi import (
    PRODUCTION_MIN_AMOUNT_COP,
    SANDBOX_MIN_AMOUNT_COP,
    WompiProvider,
    min_amount_cop,
)


def _make_payload(
    *,
    transaction_id="tx-1234",
    payment_link_id="link-abc",
    status="APPROVED",
    amount_in_cents=44900,
    timestamp=1530291411,
    reference="OC-0001",
    events_secret="test_events_secret",
    tamper=False,
    properties=None,
):
    properties = properties or ["transaction.id", "transaction.status", "transaction.amount_in_cents"]
    data = {
        "transaction": {
            "id": transaction_id,
            "payment_link_id": payment_link_id,
            "status": status,
            "amount_in_cents": amount_in_cents,
            "reference": reference,
        }
    }
    concatenated = f"{transaction_id}{status}{amount_in_cents}{timestamp}{events_secret}"
    checksum = hashlib.sha256(concatenated.encode()).hexdigest()
    if tamper:
        checksum = "0" * len(checksum)
    return {
        "event": "transaction.updated",
        "data": data,
        "signature": {"properties": properties, "checksum": checksum},
        "timestamp": timestamp,
        "sent_at": "2018-07-20T16:45:05.000Z",
    }


def _provider(events_secret: str = "test_events_secret") -> WompiProvider:
    return WompiProvider(public_key="pub", private_key="prv_test_x", events_secret=events_secret, sandbox=True)


def test_verify_webhook_signature_accepts_valid_checksum():
    payload = _make_payload()
    assert _provider().verify_webhook_signature(payload) is True


def test_verify_webhook_signature_rejects_tampered_checksum():
    payload = _make_payload(tamper=True)
    assert _provider().verify_webhook_signature(payload) is False


def test_verify_webhook_signature_rejects_wrong_secret():
    payload = _make_payload(events_secret="secret_correcto")
    assert _provider(events_secret="secret_incorrecto").verify_webhook_signature(payload) is False


def test_verify_webhook_signature_rejects_tampered_amount():
    """Si alguien intercepta y cambia el monto sin recalcular el checksum, debe fallar."""
    payload = _make_payload(amount_in_cents=44900)
    payload["data"]["transaction"]["amount_in_cents"] = 1  # monto alterado, checksum viejo
    assert _provider().verify_webhook_signature(payload) is False


def test_verify_webhook_signature_missing_fields_is_false():
    assert _provider().verify_webhook_signature({}) is False
    assert _provider().verify_webhook_signature({"signature": {}}) is False


def test_parse_webhook_event_correlates_by_payment_link_id_not_transaction_id():
    """El bug real encontrado en vivo: `provider_reference` tiene que matchear
    lo que guardamos al crear el link (`payment_link_id`), NUNCA el id de la
    transacción individual (`transaction.id`, distinto en cada intento de
    pago) -- si no, ningún webhook real encuentra jamás la fila que originó
    el pago. Confirmado contra agent-sale: `wompiWebhookHandler.ts` resuelve
    por `payment_link_id`, no por `id` ni por `reference`."""
    payload = _make_payload(status="APPROVED", transaction_id="tx-distinto-cada-intento", payment_link_id="link-abc")
    event = _provider().parse_webhook_event(payload)
    assert event.provider_reference == "link-abc"
    assert event.provider_reference != "tx-distinto-cada-intento"


def test_parse_webhook_event_maps_approved():
    payload = _make_payload(status="APPROVED")
    event = _provider().parse_webhook_event(payload)
    assert event.status == "aprobado"


def test_parse_webhook_event_maps_declined_and_error_to_rechazado():
    for raw_status in ("DECLINED", "VOIDED", "ERROR"):
        payload = _make_payload(status=raw_status)
        event = _provider().parse_webhook_event(payload)
        assert event.status == "rechazado", raw_status


def test_parse_webhook_event_unknown_status_defaults_to_pendiente():
    payload = _make_payload(status="SOME_NEW_WOMPI_STATUS")
    event = _provider().parse_webhook_event(payload)
    assert event.status == "pendiente"


@pytest.mark.asyncio
async def test_create_payment_link_rejects_amount_below_wompi_minimum_without_network_call():
    """El caso real: Pollo Broaster ($60.000) está por debajo del mínimo real
    de Wompi ($150.000 COP, confirmado por QA de agent-sale contra el
    sandbox real) -- se rechaza ANTES de llamar a la red, con un mensaje
    accionable, no con un 422 crudo."""
    provider = _provider()
    with pytest.raises(PaymentRequestRejected, match="mínimo"):
        await provider.create_payment_link(
            reference="OC-0001", amount=60_000, currency="COP", expires_in_hours=24
        )


@pytest.mark.asyncio
async def test_create_payment_link_sends_collect_shipping_and_no_reference_field(monkeypatch):
    """El body real que Wompi exige: `collect_shipping` (encontrado en vivo,
    422 real por su ausencia) y SIN `reference` (Wompi no la documenta ni la
    usa en Payment Links -- confirmado contra agent-sale, que tampoco la
    manda)."""
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": {"id": "link-xyz"}}

        text = ""

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    import pack_payments.providers.wompi as wompi_mod

    monkeypatch.setattr(wompi_mod.httpx, "AsyncClient", FakeAsyncClient)

    provider = _provider()
    result = await provider.create_payment_link(
        reference="OC-0001", amount=200_000, currency="COP", expires_in_hours=24
    )

    assert result.provider_reference == "link-xyz"
    assert captured["json"]["collect_shipping"] is False
    assert "reference" not in captured["json"]
    assert "expires_at" in captured["json"]
    assert captured["json"]["amount_in_cents"] == 20_000_000


@pytest.mark.asyncio
async def test_create_payment_link_rejection_is_recoverable_not_plugin_error(monkeypatch):
    """Un 422 real de Wompi (ej. collect_shipping/monto) debe llegar como
    PaymentRequestRejected -- NUNCA como PluginError, que dispararía
    auto_disable y apagaría pack_payments para TODOS los tenants por un
    rechazo de negocio (incidente real, 2026-09-11)."""

    class FakeResponse:
        status_code = 422
        text = '{"error":{"type":"INPUT_VALIDATION_ERROR"}}'

        def json(self):
            return {}

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResponse()

    import pack_payments.providers.wompi as wompi_mod

    monkeypatch.setattr(wompi_mod.httpx, "AsyncClient", FakeAsyncClient)

    provider = _provider()
    with pytest.raises(PaymentRequestRejected):
        await provider.create_payment_link(
            reference="OC-0001", amount=200_000, currency="COP", expires_in_hours=24
        )


def test_min_amount_depends_on_environment():
    """150.000 COP es el mínimo del SANDBOX (422 real en pruebas); en producción
    Wompi publica 1.500 COP (modelo Agregador, soporte.wompi.co)."""
    assert SANDBOX_MIN_AMOUNT_COP == 150_000
    assert PRODUCTION_MIN_AMOUNT_COP == 1_500
    assert min_amount_cop(sandbox=True) == 150_000
    assert min_amount_cop(sandbox=False) == 1_500
    assert _provider().min_amount_cop == 150_000
    assert WompiProvider("pub", "prv_prod_x", "s", sandbox=False).min_amount_cop == 1_500


@pytest.mark.asyncio
async def test_create_payment_link_production_accepts_amount_below_sandbox_minimum(monkeypatch):
    """El Pollo Broaster ($60.000) que el sandbox rechaza sí se cobra en producción."""
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"id": "link-prod"}}

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    import pack_payments.providers.wompi as wompi_mod

    monkeypatch.setattr(wompi_mod.httpx, "AsyncClient", FakeAsyncClient)

    provider = WompiProvider("pub", "prv_prod_x", "s", sandbox=False)
    result = await provider.create_payment_link(
        reference="OC-0001", amount=60_000, currency="COP", expires_in_hours=24
    )
    assert result.provider_reference == "link-prod"
    assert captured["url"].startswith("https://production.wompi.co/")
    assert captured["json"]["amount_in_cents"] == 6_000_000


@pytest.mark.asyncio
async def test_create_payment_link_production_rejects_below_published_minimum():
    provider = WompiProvider("pub", "prv_prod_x", "s", sandbox=False)
    with pytest.raises(PaymentRequestRejected, match="1,500"):
        await provider.create_payment_link(reference="OC-0001", amount=1_000, currency="COP", expires_in_hours=24)


def test_parse_webhook_event_amount_from_signed_amount_in_cents():
    payload = _make_payload(amount_in_cents=15_000_050)
    event = _provider().parse_webhook_event(payload)
    assert event.amount == 150_000.50


def test_parse_webhook_event_amount_none_when_not_signed():
    """Wompi avisa que `signature.properties` pueden variar: si el monto no está
    firmado no se confía en él (alguien pudo cambiarlo sin romper el checksum)."""
    payload = _make_payload(properties=["transaction.id", "transaction.status"])
    payload["data"]["transaction"]["amount_in_cents"] = 1
    assert _provider().parse_webhook_event(payload).amount is None


@pytest.mark.parametrize("cents", [None, "abc", 1.5, True, -100, float("nan")])
def test_parse_webhook_event_amount_none_when_invalid(cents):
    payload = _make_payload()
    payload["data"]["transaction"]["amount_in_cents"] = cents
    assert _provider().parse_webhook_event(payload).amount is None


def test_parse_webhook_event_amount_accepts_numeric_string():
    payload = _make_payload()
    payload["data"]["transaction"]["amount_in_cents"] = "4490000"
    assert _provider().parse_webhook_event(payload).amount == 44_900.0

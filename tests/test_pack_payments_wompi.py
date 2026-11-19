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
from typing import Any

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


class _FakeHttp:
    """`ScopedHttp` simulado: registra las llamadas y devuelve las respuestas en orden."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._responses = list(responses)

    async def request(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, url, kw))
        return self._responses.pop(0)


def _provider(events_secret: str = "test_events_secret", http: Any = None) -> WompiProvider:
    return WompiProvider(
        public_key="pub", private_key="prv_test_x", events_secret=events_secret, sandbox=True, http=http
    )


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
async def test_create_payment_link_sends_collect_shipping_and_no_reference_field():
    """El body real que Wompi exige: `collect_shipping` (encontrado en vivo,
    422 real por su ausencia) y SIN `reference` (Wompi no la documenta ni la
    usa en Payment Links -- confirmado contra agent-sale, que tampoco la
    manda)."""
    http = _FakeHttp({"status": 200, "data": {"data": {"id": "link-xyz"}}})

    provider = _provider(http=http)
    result = await provider.create_payment_link(
        reference="OC-0001", amount=200_000, currency="COP", expires_in_hours=24
    )

    assert result.provider_reference == "link-xyz"
    assert result.payment_link_url == "https://checkout.wompi.co/l/link-xyz"
    [(method, url, kw)] = http.calls
    assert (method, url) == ("POST", "https://sandbox.wompi.co/v1/payment_links")
    assert kw["headers"] == {"Authorization": "Bearer prv_test_x"}
    assert kw["timeout"] == 15.0
    assert kw["json_body"]["collect_shipping"] is False
    assert "reference" not in kw["json_body"]
    assert "expires_at" in kw["json_body"]
    assert kw["json_body"]["amount_in_cents"] == 20_000_000


@pytest.mark.asyncio
async def test_create_payment_link_rejection_is_recoverable_not_plugin_error():
    """Un 422 real de Wompi (ej. collect_shipping/monto) debe llegar como
    PaymentRequestRejected -- NUNCA como PluginError, que dispararía
    auto_disable y apagaría pack_payments para TODOS los tenants por un
    rechazo de negocio (incidente real, 2026-09-11)."""
    http = _FakeHttp({"status": 422, "data": {"error": {"type": "INPUT_VALIDATION_ERROR"}}})

    provider = _provider(http=http)
    with pytest.raises(PaymentRequestRejected, match=r"422 .*INPUT_VALIDATION_ERROR"):
        await provider.create_payment_link(
            reference="OC-0001", amount=200_000, currency="COP", expires_in_hours=24
        )


@pytest.mark.asyncio
async def test_create_payment_link_sin_id_es_rechazo_recuperable():
    """Un 2xx sin `data.id` (o con cuerpo que no es JSON) no revienta: rechazo recuperable."""
    for body in ({"data": {}}, "<html>bad gateway</html>"):
        with pytest.raises(PaymentRequestRejected, match="sin id"):
            await _provider(http=_FakeHttp({"status": 200, "data": body})).create_payment_link(
                reference="OC-0001", amount=200_000, currency="COP", expires_in_hours=24
            )


@pytest.mark.asyncio
async def test_create_payment_link_fallo_de_red_es_plugin_error():
    """Si la llamada no llegó a Wompi (red, dominio fuera de la allowlist) es una falla
    de infraestructura: `PluginError`, como antes la excepción de httpx que el host
    envolvía. No es un rechazo de negocio."""
    from core.shared.exceptions import PluginError

    http = _FakeHttp({"error": "Fallo de red: ReadTimeout"})
    with pytest.raises(PluginError, match="Fallo de red: ReadTimeout"):
        await _provider(http=http).create_payment_link(
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
async def test_create_payment_link_production_accepts_amount_below_sandbox_minimum():
    """El Pollo Broaster ($60.000) que el sandbox rechaza sí se cobra en producción."""
    http = _FakeHttp({"status": 200, "data": {"data": {"id": "link-prod"}}})

    provider = WompiProvider("pub", "prv_prod_x", "s", sandbox=False, http=http)
    result = await provider.create_payment_link(
        reference="OC-0001", amount=60_000, currency="COP", expires_in_hours=24
    )
    assert result.provider_reference == "link-prod"
    [(_, url, kw)] = http.calls
    assert url.startswith("https://production.wompi.co/")
    assert kw["json_body"]["amount_in_cents"] == 6_000_000


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

"""WompiProvider: verificación de firma de webhook y parseo de eventos.

La verificación de firma es la parte de seguridad crítica de pack-payments
(un webhook sin verificar dejaría marcar pedidos como pagados a cualquiera
que le pegue a la URL) -- se prueba con un checksum calculado a mano con el
mismo algoritmo documentado por Wompi (docs.wompi.co/docs/colombia/eventos):
concatenar los valores de `signature.properties` (en orden, sin separador)
+ `timestamp` + `events_secret`, SHA256, comparar contra `signature.checksum`.
"""

from __future__ import annotations

import hashlib

from pack_payments.providers.wompi import WompiProvider


def _make_payload(*, transaction_id="1234", status="APPROVED", amount_in_cents=44900, timestamp=1530291411,
                   reference="OC-0001", events_secret="test_events_secret", tamper=False):
    properties = ["transaction.id", "transaction.status", "transaction.amount_in_cents"]
    data = {
        "transaction": {
            "id": transaction_id,
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
    return WompiProvider(public_key="pub", private_key="prv", events_secret=events_secret, sandbox=True)


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


def test_parse_webhook_event_maps_approved():
    payload = _make_payload(status="APPROVED", reference="OC-0042", transaction_id="tx-1")
    event = _provider().parse_webhook_event(payload)
    assert event.status == "aprobado"
    assert event.reference == "OC-0042"
    assert event.provider_reference == "tx-1"


def test_parse_webhook_event_maps_declined_and_error_to_rechazado():
    for raw_status in ("DECLINED", "VOIDED", "ERROR"):
        payload = _make_payload(status=raw_status)
        event = _provider().parse_webhook_event(payload)
        assert event.status == "rechazado", raw_status


def test_parse_webhook_event_unknown_status_defaults_to_pendiente():
    payload = _make_payload(status="SOME_NEW_WOMPI_STATUS")
    event = _provider().parse_webhook_event(payload)
    assert event.status == "pendiente"

"""Plantillas de Twilio (ADR-0186 §8, PR-6), sin base: validación, backoff del polling y cómo se aplica un estado."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from agentcore_channel_twilio.content import Approval, ContentTemplate

from core.db.models import TwilioContentTemplate
from core.gateway.twilio import plantillas
from core.gateway.twilio.alta import AltaError

AHORA = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("Recordatorio", "es", "UTILITY", "Hola", {}), "nombre_invalido"),
        (("recordatorio", "español", "UTILITY", "Hola", {}), "idioma_invalido"),
        (("recordatorio", "es", "PROMO", "Hola", {}), "categoria_invalida"),
        (("recordatorio", "es", "UTILITY", "   ", {}), "cuerpo_invalido"),
        (("recordatorio", "es", "UTILITY", "x" * 1025, {}), "cuerpo_invalido"),
        (
            ("recordatorio", "es", "UTILITY", "Hola {{1}} y {{3}}", {"1": "a", "3": "c"}),
            "variables_invalidas",
        ),
        (("recordatorio", "es", "UTILITY", "Hola {{1}}", {}), "variables_invalidas"),
    ],
)
def test_validar_rechaza(args, code):
    with pytest.raises(AltaError) as exc:
        plantillas.validar(*args)
    assert (exc.value.http_status, exc.value.code) == (422, code)


def test_validar_acepta():
    plantillas.validar(
        "recordatorio_cita",
        "es_CO",
        "UTILITY",
        "Hola {{1}}, tu cita es el {{2}}",
        {"1": "Ana", "2": "lunes"},
    )


@pytest.mark.parametrize(
    ("espera", "siguiente"),
    [
        (None, timedelta(minutes=5)),
        (timedelta(minutes=10), timedelta(minutes=5)),
        (timedelta(hours=3), timedelta(minutes=30)),
        (timedelta(days=5), timedelta(hours=1)),
        (timedelta(days=15), None),
    ],
)
def test_backoff_del_polling(espera, siguiente):
    enviada = None if espera is None else AHORA - espera
    proximo = plantillas.proximo_poll(enviada, AHORA)
    assert proximo == (None if siguiente is None else AHORA + siguiente)


def _fila(estado="received") -> TwilioContentTemplate:
    return TwilioContentTemplate(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        channel_connection_id=uuid.uuid4(),
        friendly_name="a",
        whatsapp_name="a",
        language="es",
        category="UTILITY",
        body="Hola",
        approval_status=estado,
        submitted_at=AHORA - timedelta(minutes=20),
        rejection_reason="",
    )


def test_aplicar_aprobacion():
    fila = _fila()
    plantillas._aplicar_aprobacion(fila, "pending", "", "MARKETING", AHORA)
    assert (fila.approval_status, fila.category, fila.next_poll_at) == (
        "pending",
        "MARKETING",
        AHORA + timedelta(minutes=5),
    )
    plantillas._aplicar_aprobacion(fila, "approved", "", "", AHORA)
    assert (fila.approval_status, fila.approved_at, fila.next_poll_at) == ("approved", AHORA, None)
    fila = _fila()
    plantillas._aplicar_aprobacion(fila, "rejected", "INVALID_FORMAT", "", AHORA)
    assert (fila.approval_status, fila.rejection_reason, fila.next_poll_at) == (
        "rejected",
        "INVALID_FORMAT",
        None,
    )
    fila = _fila()
    plantillas._aplicar_aprobacion(
        fila, "algo_nuevo", "", "", AHORA
    )  # estado desconocido: no se toca
    assert fila.approval_status == "received"


def test_desde_twilio_omite_lo_que_no_cabe():
    base = dict(
        sid="HX" + "a" * 32,
        language="es",
        variables={},
        approval=Approval("approved", name="x", category="UTILITY"),
    )
    ok = plantillas._desde_twilio(
        uuid.uuid4(),
        uuid.uuid4(),
        ContentTemplate(friendly_name="hola", types={"twilio/text": {"body": "Hola"}}, **base),
    )
    assert ok is not None and ok.body == "Hola" and ok.category == "UTILITY"
    assert (
        plantillas._desde_twilio(
            uuid.uuid4(),
            uuid.uuid4(),
            ContentTemplate(friendly_name="Con Espacios", types={}, **base),
        )
        is None
    )

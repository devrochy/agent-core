"""Sentry no recibe el PIN de un número ni secretos de Meta (`sentry_setup._event_scrubber`).

El PIN de verificación en dos pasos viaja en el cuerpo de `POST .../numbers/{id}/register` y
en locales como `pin` o `body`: una excepción no controlada en esa ruta lo mandaría a Sentry.
"""

from __future__ import annotations

import json

import core.observability.sentry_setup as sentry_setup
from core.observability.sentry_setup import _event_scrubber


def test_configure_sentry_instala_el_scrubber(monkeypatch):
    import sentry_sdk

    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "sentry_enabled", True)
    monkeypatch.setattr(settings, "sentry_dsn", "https://clave@sentry.invalid/1")
    monkeypatch.setattr(sentry_setup, "_configured", False)
    llamadas: list[dict] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: llamadas.append(kwargs))

    sentry_setup.configure_sentry()

    (kwargs,) = llamadas
    event = {"request": {"data": {"pin": "246810"}}}
    kwargs["event_scrubber"].scrub_event(event)
    assert "246810" not in json.dumps(event, default=str)


def test_una_excepcion_capturada_no_lleva_el_pin_del_request_ni_el_guardado():
    # Captura real de sentry-sdk: los locales se serializan (un modelo pydantic queda como su
    # repr, en texto) y después pasa el scrubber.
    from sentry_sdk.utils import event_from_exception

    from core.api.routers.meta_ops import RegisterNumberRequest

    # Calculados: el evento también lleva el código fuente alrededor de cada frame, y un PIN
    # escrito literal en el test aparecería ahí (en producción el PIN nunca está en el código).
    pin_del_request, pin_guardado = str(123 * 2006), str(271 * 501)

    def falla_con_secretos_en_el_frame(json_body: dict, previous_pin: str):
        # Como en la ruta y el servicio: el modelo del request, el PIN guardado y el cuerpo a Meta.
        payload = RegisterNumberRequest.model_validate(json_body)
        body = {"messaging_product": "whatsapp", "pin": json_body["pin"]}
        raise RuntimeError(f"Redis caído ({payload.model_fields_set}, {len(previous_pin)}, {len(body)})")

    try:
        falla_con_secretos_en_el_frame({"pin": pin_del_request}, pin_guardado)
    except RuntimeError as exc:
        error = exc
    del pin_del_request, pin_guardado  # que no queden en los locales del test
    event, _hint = event_from_exception(error)

    _event_scrubber().scrub_event(event)

    crudo = json.dumps(event, default=str)
    assert str(123 * 2006) not in crudo
    assert str(271 * 501) not in crudo


def test_el_pin_y_los_secretos_no_llegan_a_sentry():
    event = {
        "request": {"data": {"pin": "246810", "nota": "visible"}},
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "pin": "246810",
                                    "new_pin": "135790",
                                    "body": {"messaging_product": "whatsapp", "pin": "246810"},
                                    "creds": {"app_secret": "s3cr3t-app", "access_token": "tok-abc"},
                                    "phone_number_id": "123",
                                }
                            }
                        ]
                    }
                }
            ]
        },
    }

    _event_scrubber().scrub_event(event)

    crudo = json.dumps(event, default=str)
    for secreto in ("246810", "135790", "s3cr3t-app", "tok-abc"):
        assert secreto not in crudo
    assert event["request"]["data"]["nota"] == "visible"
    assert event["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]["phone_number_id"] == "123"

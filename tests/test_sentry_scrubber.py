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

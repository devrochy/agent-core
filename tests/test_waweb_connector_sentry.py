"""El conector de WhatsApp Web configura Sentry con `role=connector` (B6, ADR-0188)."""

from __future__ import annotations

import core.observability.sentry_setup as sentry_setup
import core.waweb.connector as connector
from core.config import get_settings


def test_main_configura_sentry_con_el_rol(monkeypatch):
    import sentry_sdk

    settings = get_settings()
    monkeypatch.setattr(settings, "sentry_enabled", True)
    monkeypatch.setattr(settings, "sentry_dsn", "https://clave@sentry.invalid/1")
    monkeypatch.setattr(settings, "waweb_enabled", False)  # run_connector sale enseguida
    monkeypatch.setattr(sentry_setup, "_configured", False)
    inits: list[dict] = []
    tags: list[tuple[str, str]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: inits.append(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: tags.append((k, v)))
    monkeypatch.setattr(connector.signal, "signal", lambda *_a: None)

    connector.main()

    assert len(inits) == 1
    assert "event_scrubber" in inits[0]  # el mismo scrubber que la API y el worker
    assert tags == [("role", "connector")]


def test_sin_sentry_no_hay_tag(monkeypatch):
    import sentry_sdk

    monkeypatch.setattr(get_settings(), "sentry_enabled", False)
    monkeypatch.setattr(sentry_setup, "_configured", False)
    tags: list[tuple[str, str]] = []
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: tags.append((k, v)))

    connector._configurar_sentry()

    assert tags == []


def test_las_variables_del_conector_no_salen_a_sentry():
    import json

    from core.observability.sentry_setup import _event_scrubber

    evento = {
        "exception": {"values": [{"stacktrace": {"frames": [{"vars": {
            "event": "InboundEvent(text='hola, mi cédula es 123', sender='573001234567')",
            "qr": "2@codigo-de-vinculacion", "job": {"text": "x", "to": "573001234567"},
            "jid": "573001234567@s.whatsapp.net", "sender": "573001234567", "raw": "{}",
        }}]}}]}
    }
    _event_scrubber().scrub_event(evento)
    texto = json.dumps(evento, default=str)
    assert "573001234567" not in texto and "codigo-de-vinculacion" not in texto and "cédula" not in texto

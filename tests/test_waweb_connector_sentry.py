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

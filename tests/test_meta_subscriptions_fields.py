"""Normalización de `fields` de la Graph API en el health-check de suscripciones.

Regresión: Meta devuelve `fields` como objetos `{"name","version"}` (API v26);
el código los pasaba a una columna `text[]` y el cron reventaba.
"""

from __future__ import annotations

from core.tasks.meta_subscriptions_health import _normalize_fields


def test_normaliza_objetos_a_nombres():
    raw = [{"name": "messages", "version": "v26.0"}, {"name": "account_update", "version": "v26.0"}]
    assert _normalize_fields(raw) == ["messages", "account_update"]


def test_acepta_strings_legados():
    assert _normalize_fields(["messages", "account_update"]) == ["messages", "account_update"]


def test_tolera_entradas_raras():
    assert _normalize_fields(None) == []
    assert _normalize_fields("messages") == []
    assert _normalize_fields([{"version": "v26.0"}, "", None, 7]) == ["7"]

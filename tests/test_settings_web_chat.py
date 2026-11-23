"""Chat web: normalización de dominios y salida segura (HU-CHAN-02)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.api.errors import ApiError
from core.api.routers.settings import (
    _normalize_origins,
    _normalize_turnstile_site_key,
    _web_chat_out,
)


def test_normalize_origins_quita_barras_y_duplicados():
    assert _normalize_origins(
        ["https://negocio.com/", "https://NEGOCIO.com", "https://b.com", "", "  "]
    ) == ["https://negocio.com", "https://b.com"]


def test_normalize_origins_rechaza_esquemas_invalidos():
    assert _normalize_origins(["javascript:alert(1)", "negocio.com", "ftp://x.com"]) == []
    assert _normalize_origins(["http://localhost:3000"]) == ["http://localhost:3000"]


def test_web_chat_out_no_expone_el_secreto():
    app_settings = SimpleNamespace(
        web_chat_config={
            "enabled": True,
            "key": "clave-publica-123",
            "allowed_origins": ["https://negocio.com"],
            "turnstile_site_key": "0x4AAAAAAA_site_key",
            "turnstile_secret": "secreto-turnstile",
        }
    )
    out = _web_chat_out(app_settings)
    assert out.enabled is True
    assert out.key == "clave-publica-123"
    assert out.turnstile_configured is True
    # La site key es pública y el panel la necesita para mostrarla; el secreto no sale.
    assert out.turnstile_site_key == "0x4AAAAAAA_site_key"
    assert "secreto-turnstile" not in out.model_dump_json()
    assert "/widget.js" in out.snippet and 'data-key="clave-publica-123"' in out.snippet


def test_web_chat_out_sin_key_no_arma_snippet():
    out = _web_chat_out(None)
    assert out.enabled is False
    assert out.key == ""
    assert out.snippet == ""
    assert out.turnstile_configured is False
    assert out.turnstile_site_key == ""


def test_normalize_turnstile_site_key_valida_formato():
    assert _normalize_turnstile_site_key(None) is None
    assert _normalize_turnstile_site_key("  ") == ""
    assert _normalize_turnstile_site_key(" 0x4AAAAAAA_site_key ") == "0x4AAAAAAA_site_key"
    with pytest.raises(ApiError) as exc:
        _normalize_turnstile_site_key("no-es-una-site-key")
    assert (exc.value.status_code, exc.value.code) == (422, "turnstile_site_key")

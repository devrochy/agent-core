"""Elegir una ruta de Instagram nunca arrastra los permisos/credenciales de
la otra -- criterio de aceptación explícito del ticket."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_instagram as ig_mod
from core.gateway.meta_instagram import (
    InstagramError,
    build_authorize_url,
    sign_state,
    verify_state,
)


def _settings(**overrides):
    base = dict(
        instagram_app_id="ig-app-999", instagram_app_secret="ig-secret-nunca-mezclar",
        meta_app_id="fb-app-111", meta_app_secret="fb-secret-nunca-mezclar",
        meta_config_id="cfg-messenger", meta_config_id_instagram="cfg-instagram-dedicado",
        app_secret_key="fallback", app_env="development",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_authorize_url_usa_solo_credenciales_de_instagram(monkeypatch):
    monkeypatch.setattr(ig_mod, "get_settings", _settings)
    url = build_authorize_url("https://panel.example.test/callback", "state-123")

    assert "ig-app-999" in url
    assert "fb-app-111" not in url, "la ruta (b) nunca debe llevar el App ID de Facebook"
    assert "instagram_business_basic" in url
    assert "instagram_business_manage_messages" in url
    assert "pages_show_list" not in url, "la ruta (b) nunca pide permisos de la ruta (a)"
    assert "enable_fb_login=false" in url


def test_state_firmado_y_verificado_correctamente(monkeypatch):
    monkeypatch.setattr(ig_mod, "get_settings", _settings)
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    state = sign_state(tenant_id, admin_id)

    resolved = verify_state(state, cookie_value=state)
    assert resolved == tenant_id


def test_state_no_coincide_con_cookie_falla(monkeypatch):
    monkeypatch.setattr(ig_mod, "get_settings", _settings)
    state = sign_state(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(InstagramError, match="CSRF"):
        verify_state(state, cookie_value="algo-distinto")


def test_state_firmado_con_otro_secreto_falla(monkeypatch):
    monkeypatch.setattr(ig_mod, "get_settings", _settings)
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    state = sign_state(tenant_id, admin_id)

    # Cambia el secreto -- simula un intento de forjar el state.
    monkeypatch.setattr(ig_mod, "get_settings", lambda: _settings(instagram_app_secret="otro-secreto"))
    with pytest.raises(InstagramError, match=r"[Ff]irma"):
        verify_state(state, cookie_value=state)


def test_object_fields_de_las_dos_rutas_no_se_mezclan():
    """`OBJECT_FIELDS`/scopes de la ruta (a) (Facebook Login: instagram_basic,
    pages_*) no deben aparecer en ningún literal de la ruta (b)."""
    import inspect

    source_b = inspect.getsource(ig_mod.build_authorize_url) + inspect.getsource(ig_mod.exchange_short_lived_code)
    for forbidden in ("pages_show_list", "pages_read_engagement", "pages_manage_metadata", "instagram_manage_messages"):
        assert forbidden not in source_b

"""`exchange_code()` es time-critical: el `code` de Meta tiene TTL de 30s.
Se mockea httpx/get_session/meta_token_store para no tocar la BD/red real
-- mismo estilo que tests/test_meta_preflight.py y tests/test_meta_subscriptions.py."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

import core.gateway.meta_onboarding as onboarding_mod
from core.gateway.meta_errors import GraphError


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, handler):
        self._handler = handler

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kwargs):
        return self._handler(url, params or {})


class FakeSession:
    def __init__(self, connection):
        self._connection = connection

    async def get(self, model, id_):
        return self._connection

    async def flush(self):
        return None


class FakeSessionCM:
    def __init__(self, connection):
        self._connection = connection

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeSession(self._connection)

    async def __aexit__(self, *a):
        return False


def _fake_wizard(connection_id):
    return SimpleNamespace(
        id=uuid.uuid4(), connection_id=connection_id, tenant_id=uuid.uuid4(), channel="whatsapp",
        environment="production", current_step="exchange_code", completed_steps=[], last_error=None, context={},
    )


def _settings():
    return SimpleNamespace(
        meta_app_id="app-1", meta_app_secret="s3cr3t", meta_config_id="cfg-1",
        graph_api_version="v21.0", public_base_url="https://panel.example.test",
    )


def _patch_common(monkeypatch, oauth_handler, *, wizard, connection):
    monkeypatch.setattr(onboarding_mod, "get_current_tenant_id", lambda: wizard.tenant_id)
    monkeypatch.setattr(onboarding_mod, "get_settings", _settings)
    monkeypatch.setattr(onboarding_mod, "_get_wizard", _fake_get_wizard(wizard))
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(connection))

    async def _fake_get_owned(session, model, id_, tenant_id=None):
        return wizard

    monkeypatch.setattr(onboarding_mod, "get_owned", _fake_get_owned)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient(oauth_handler))


def _fake_get_wizard(wizard):
    async def _inner(wizard_id, tenant_id=None):
        return wizard

    return _inner


@pytest.mark.asyncio
async def test_canje_sin_redirect_uri_primero(monkeypatch):
    wizard = _fake_wizard(uuid.uuid4())
    connection = SimpleNamespace(business_id="biz-1", status="draft")
    captured = {}

    def handler(url, params):
        captured["params"] = params
        return FakeResponse(json_data={"access_token": "tok-real-no-debe-salir"})

    _patch_common(monkeypatch, handler, wizard=wizard, connection=connection)

    put_calls = []

    async def _fake_put_token(connection_id, kind, subject_id, token, **kwargs):
        put_calls.append((connection_id, kind, subject_id, token))

    monkeypatch.setattr(onboarding_mod.meta_token_store, "put_token", _fake_put_token)

    result = await onboarding_mod.exchange_code(wizard.id, "code-de-un-solo-uso")

    assert "redirect_uri" not in captured["params"]
    assert put_calls == [(wizard.connection_id, "business_integration_system_user", "biz-1", "tok-real-no-debe-salir")]
    assert result.current_step == "discover_assets"
    # el token nunca aparece en la vista devuelta
    assert "tok-real-no-debe-salir" not in str(vars(result))


@pytest.mark.asyncio
async def test_canje_reintenta_con_redirect_uri_si_meta_lo_menciona(monkeypatch):
    wizard = _fake_wizard(uuid.uuid4())
    connection = SimpleNamespace(business_id="biz-2", status="draft")
    calls = []

    def handler(url, params):
        calls.append(params)
        if "redirect_uri" not in params:
            return FakeResponse(
                status_code=400,
                json_data={"error": {"message": "Missing redirect_uri parameter.", "type": "OAuthException", "code": 100}},
            )
        return FakeResponse(json_data={"access_token": "tok-segundo-intento"})

    _patch_common(monkeypatch, handler, wizard=wizard, connection=connection)
    monkeypatch.setattr(onboarding_mod.meta_token_store, "put_token", _noop_put_token)

    result = await onboarding_mod.exchange_code(wizard.id, "code-x")

    assert len(calls) == 2
    assert "redirect_uri" not in calls[0]
    assert calls[1]["redirect_uri"].endswith("/admin/conexiones/wizard/whatsapp")
    assert result.current_step == "discover_assets"


@pytest.mark.asyncio
async def test_canje_propaga_graph_error_sin_redirect_uri_si_no_lo_menciona(monkeypatch):
    wizard = _fake_wizard(uuid.uuid4())
    connection = SimpleNamespace(business_id="biz-3", status="draft")

    def handler(url, params):
        return FakeResponse(
            status_code=400,
            json_data={"error": {"message": "This authorization code has expired.", "type": "OAuthException", "code": 100}},
        )

    _patch_common(monkeypatch, handler, wizard=wizard, connection=connection)
    monkeypatch.setattr(onboarding_mod.meta_token_store, "put_token", _noop_put_token)

    with pytest.raises(GraphError):
        await onboarding_mod.exchange_code(wizard.id, "code-expirado")


@pytest.mark.asyncio
async def test_despacho_de_la_llamada_a_meta_ocurre_en_menos_de_5_segundos(monkeypatch):
    """Criterio de aceptación del ticket: el canje ocurre en <30s desde el
    FINISH; este test es la guarda de regresión más estricta (falla si el
    dispatch de la llamada HTTP tarda más de 5s desde que se recibió el
    code) -- protege contra que alguien inserte una cola/job en el medio."""
    wizard = _fake_wizard(uuid.uuid4())
    connection = SimpleNamespace(business_id="biz-4", status="draft")

    def handler(url, params):
        return FakeResponse(json_data={"access_token": "tok"})

    _patch_common(monkeypatch, handler, wizard=wizard, connection=connection)
    monkeypatch.setattr(onboarding_mod.meta_token_store, "put_token", _noop_put_token)

    captured_log = {}

    def _fake_log_info(event, **kwargs):
        if event == "embedded_signup_exchange_latency":
            captured_log.update(kwargs)

    monkeypatch.setattr(onboarding_mod.log, "info", _fake_log_info)

    await onboarding_mod.exchange_code(wizard.id, "code-y")

    assert captured_log["dispatch_ms"] < 5000, "el dispatch de la llamada a Meta tardó más de 5s"


async def _noop_put_token(connection_id, kind, subject_id, token, **kwargs):
    return None

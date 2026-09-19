"""run_preflight(): checks contra Graph API con httpx mockeado (mismo estilo
que tests/test_pack_payments_wompi.py: FakeAsyncClient vía monkeypatch)."""

from __future__ import annotations

import httpx
import pytest

import core.gateway.meta_preflight as preflight_mod
from core.gateway.meta_app_config import MetaAppConfig

APP_SECRET = "s3cr3t-de-prueba-que-nunca-debe-aparecer"
ACCESS_TOKEN = "token-de-prueba-que-nunca-debe-aparecer"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class FakeAsyncClient:
    """`handler(url, params) -> FakeResponse`, o levanta la excepción que le pasen."""

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


def _config():
    return MetaAppConfig(app_id="123456789", app_secret=APP_SECRET, config_id="987654321")


def _patch_common(monkeypatch, handler, *, verify_token="tok-verify"):
    monkeypatch.setattr(preflight_mod.httpx, "AsyncClient", FakeAsyncClient(handler))
    monkeypatch.setattr(preflight_mod, "effective_meta_app_config", lambda tenant_id=None: _config())

    async def _fake_config(*a, **k):
        return _config()

    monkeypatch.setattr(preflight_mod, "effective_meta_app_config", _fake_config)

    async def _fake_credentials(*a, **k):
        return {"verify_token": verify_token}

    monkeypatch.setattr(preflight_mod, "effective_credentials", _fake_credentials)
    monkeypatch.setattr("core.tenants.tenant_webhook_url", lambda slug: "https://example.test/webhooks/whatsapp/x")


def _handler_success(url, params):
    if "oauth/access_token" in url:
        return FakeResponse(json_data={"access_token": ACCESS_TOKEN})
    if url.endswith("/123456789"):
        return FakeResponse(json_data={"name": "Mi App", "category": "Business", "app_type": 0})
    if url.endswith("/subscriptions"):
        return FakeResponse(
            json_data={
                "data": [
                    {"object": "whatsapp_business_account", "callback_url": "https://x/webhook", "active": True, "fields": ["messages"]}
                ]
            }
        )
    if "webhooks/whatsapp" in url:
        return FakeResponse(status_code=200, text=params.get("hub.challenge", ""))
    raise AssertionError(f"URL no esperada: {url}")


@pytest.mark.asyncio
async def test_preflight_camino_feliz(monkeypatch):
    _patch_common(monkeypatch, _handler_success)
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    by_key = {c.key: c for c in checks}
    assert by_key["app_token"].ok is True
    assert by_key["app_info"].ok is True
    assert "Mi App" in by_key["app_info"].detail
    assert by_key["app_subscriptions"].ok is True
    assert by_key["config_id"].ok is True
    assert by_key["webhook_handshake"].ok is True


@pytest.mark.asyncio
async def test_preflight_app_secret_incorrecto_190(monkeypatch):
    def handler(url, params):
        if "oauth/access_token" in url:
            return FakeResponse(status_code=401, json_data={"error": {"code": 190, "message": "Invalid secret"}})
        if "webhooks/whatsapp" in url:
            return FakeResponse(status_code=200, text=params.get("hub.challenge", ""))
        raise AssertionError(f"no debería llamarse: {url}")

    _patch_common(monkeypatch, handler)
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    by_key = {c.key: c for c in checks}
    assert by_key["app_token"].ok is False
    assert "META_APP_SECRET" in by_key["app_token"].detail
    # sin token válido, los checks dependientes se saltan (no llaman a Meta de nuevo)
    assert by_key["app_info"].ok is False
    assert by_key["app_subscriptions"].ok is False


@pytest.mark.asyncio
async def test_preflight_rate_limit_no_reintenta(monkeypatch):
    calls = {"n": 0}

    def handler(url, params):
        if "oauth/access_token" in url:
            calls["n"] += 1
            return FakeResponse(status_code=400, json_data={"error": {"code": 4, "message": "App request limit reached"}})
        if "webhooks/whatsapp" in url:
            return FakeResponse(status_code=200, text=params.get("hub.challenge", ""))
        raise AssertionError(f"no debería llamarse: {url}")

    _patch_common(monkeypatch, handler)
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    by_key = {c.key: c for c in checks}
    assert by_key["app_token"].ok is None
    assert calls["n"] == 1, "no debe reintentar en bucle ante un rate limit"


@pytest.mark.asyncio
async def test_preflight_webhook_timeout_menciona_https(monkeypatch):
    def handler(url, params):
        if "oauth/access_token" in url:
            return FakeResponse(json_data={"access_token": ACCESS_TOKEN})
        if url.endswith("/123456789"):
            return FakeResponse(json_data={"name": "Mi App", "category": "x", "app_type": 0})
        if url.endswith("/subscriptions"):
            return FakeResponse(json_data={"data": []})
        if "webhooks/whatsapp" in url:
            raise httpx.TimeoutException("timeout")
        raise AssertionError(f"URL no esperada: {url}")

    _patch_common(monkeypatch, handler)
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    by_key = {c.key: c for c in checks}
    assert by_key["webhook_handshake"].ok is False
    assert "HTTPS" in by_key["webhook_handshake"].detail
    assert "autofirmado" in by_key["webhook_handshake"].detail


@pytest.mark.asyncio
async def test_preflight_sin_config_no_rompe(monkeypatch):
    async def _no_config(*a, **k):
        return None

    monkeypatch.setattr(preflight_mod, "effective_meta_app_config", _no_config)

    async def _fake_credentials(*a, **k):
        return {"verify_token": "tok"}

    monkeypatch.setattr(preflight_mod, "effective_credentials", _fake_credentials)
    monkeypatch.setattr("core.tenants.tenant_webhook_url", lambda slug: "https://example.test/webhooks/whatsapp/x")
    monkeypatch.setattr(
        preflight_mod.httpx,
        "AsyncClient",
        FakeAsyncClient(lambda url, params: FakeResponse(status_code=200, text=params.get("hub.challenge", ""))),
    )

    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    by_key = {c.key: c for c in checks}
    assert by_key["app_token"].ok is False
    assert by_key["config_id"].ok is False
    assert by_key["webhook_handshake"].ok is True  # el handshake no depende de la app config


@pytest.mark.asyncio
async def test_preflight_nunca_expone_secretos(monkeypatch):
    """Criterio de aceptación: ningún check incluye el App Secret ni el token."""
    _patch_common(monkeypatch, _handler_success)
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    for check in checks:
        assert APP_SECRET not in check.detail
        assert ACCESS_TOKEN not in check.detail
        blob = check.model_dump_json()
        assert APP_SECRET not in blob
        assert ACCESS_TOKEN not in blob

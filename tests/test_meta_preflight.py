"""run_preflight(): checks contra Graph API.

`_fetch_app_token` y `_check_webhook_handshake` siguen usando `httpx` directo
(mockeado con `FakeAsyncClient`, mismo estilo que tests/test_pack_payments_wompi.py).
`_check_app_info`/`_check_subscriptions` pasan por el cliente único
(`core.gateway.meta_graph.graph_fetch`), que además escribe en
`graph_api_calls`/`graph_api_usage` -- se mockea `graph_fetch` directamente
para no tocar la BD desde un test unitario."""

from __future__ import annotations

import httpx
import pytest

import core.gateway.meta_preflight as preflight_mod
from core.gateway.meta_errors import GraphError
from core.gateway.meta_preflight import AppCredentials

APP_SECRET = "s3cr3t-de-prueba-que-nunca-debe-aparecer"
ACCESS_TOKEN = "token-de-prueba-que-nunca-debe-aparecer"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = {}

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
    return AppCredentials(
        app_id="123456789", app_secret=APP_SECRET, config_id="987654321", graph_api_version="v21.0"
    )


def _fake_graph_fetch(success_by_path: dict[str, dict], error_by_path: dict[str, GraphError] | None = None):
    """Reemplazo de `graph_fetch()` para `_check_app_info`/`_check_subscriptions`:
    matchea por sufijo de `path` (no arma URL real, no toca la BD)."""
    error_by_path = error_by_path or {}

    async def _fake(path, **kwargs):
        for suffix, error in error_by_path.items():
            if path.endswith(suffix):
                raise error
        for suffix, data in success_by_path.items():
            if path.endswith(suffix):
                return data
        raise AssertionError(f"path no esperado en graph_fetch: {path}")

    return _fake


def _patch_common(monkeypatch, oauth_handler, *, verify_token="tok-verify", graph_fetch_impl=None):
    monkeypatch.setattr(preflight_mod.httpx, "AsyncClient", FakeAsyncClient(oauth_handler))

    async def _fake_config(*a, **k):
        return _config()

    monkeypatch.setattr(preflight_mod, "resolve_app_credentials", _fake_config)

    if graph_fetch_impl is not None:
        monkeypatch.setattr(preflight_mod, "graph_fetch", graph_fetch_impl)

    async def _fake_verify_token(*a, **k):
        return verify_token

    monkeypatch.setattr("core.gateway.connections.resolve_effective_verify_token", _fake_verify_token)
    monkeypatch.setattr("core.tenants.tenant_webhook_url", lambda slug: "https://example.test/webhooks/whatsapp/x")


def _oauth_success(url, params):
    if "oauth/access_token" in url:
        return FakeResponse(json_data={"access_token": ACCESS_TOKEN})
    if "webhooks/whatsapp" in url:
        return FakeResponse(status_code=200, text=params.get("hub.challenge", ""))
    raise AssertionError(f"URL no esperada: {url}")


_GRAPH_SUCCESS = _fake_graph_fetch(
    {
        "/123456789": {"name": "Mi App", "category": "Business", "app_type": 0},
        "/subscriptions": {
            "data": [
                {"object": "whatsapp_business_account", "callback_url": "https://x/webhook", "active": True, "fields": ["messages"]}
            ]
        },
    }
)


@pytest.mark.asyncio
async def test_preflight_camino_feliz(monkeypatch):
    _patch_common(monkeypatch, _oauth_success, graph_fetch_impl=_GRAPH_SUCCESS)
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
        if "webhooks/whatsapp" in url:
            raise httpx.TimeoutException("timeout")
        raise AssertionError(f"URL no esperada: {url}")

    _patch_common(
        monkeypatch,
        handler,
        graph_fetch_impl=_fake_graph_fetch(
            {"/123456789": {"name": "Mi App", "category": "x", "app_type": 0}, "/subscriptions": {"data": []}}
        ),
    )
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    by_key = {c.key: c for c in checks}
    assert by_key["webhook_handshake"].ok is False
    assert "HTTPS" in by_key["webhook_handshake"].detail
    assert "autofirmado" in by_key["webhook_handshake"].detail


@pytest.mark.asyncio
async def test_preflight_sin_config_no_rompe(monkeypatch):
    async def _no_config(*a, **k):
        return None

    monkeypatch.setattr(preflight_mod, "resolve_app_credentials", _no_config)

    async def _fake_verify_token(*a, **k):
        return "tok"

    monkeypatch.setattr("core.gateway.connections.resolve_effective_verify_token", _fake_verify_token)
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
    _patch_common(monkeypatch, _oauth_success, graph_fetch_impl=_GRAPH_SUCCESS)
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme")
    for check in checks:
        assert APP_SECRET not in check.detail
        assert ACCESS_TOKEN not in check.detail
        blob = check.model_dump_json()
        assert APP_SECRET not in blob
        assert ACCESS_TOKEN not in blob


@pytest.mark.asyncio
async def test_sin_app_propia_el_negocio_no_ve_los_datos_de_la_app_de_la_plataforma(monkeypatch):
    """HU-CONN-07: la app de la plataforma la revisa solo el master (Plataforma → App de
    Meta). Un negocio sin app propia ve solo el handshake de su webhook."""

    def _graph_prohibido(*a, **k):
        raise AssertionError("no debe consultar la app de la plataforma")

    _patch_common(monkeypatch, _oauth_success, graph_fetch_impl=_graph_prohibido)
    checks = await preflight_mod.run_preflight(tenant_id="t1", tenant_slug="acme", include_platform_app=False)

    assert [c.key for c in checks] == ["platform_app", "webhook_handshake"]
    assert checks[0].ok is None
    assert "Mi App" not in checks[0].detail
    assert checks[1].ok is True

"""Cliente único de Graph API (core/gateway/meta_graph.py). No toca la BD:
`_log_call`/`record_usage` se mockean (unitario puro, mismo criterio que el
resto de tests/ -- la persistencia real se cubre en tests/integration/)."""

from __future__ import annotations

import pytest

import core.gateway.meta_graph as graph_mod
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import appsecret_proof, graph_fetch

APP_SECRET = "secreto-de-prueba"
TOKEN = "token-de-prueba"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, params=None, json=None, headers=None):
        call = {"method": method, "url": url, "params": params or {}, "json": json, "headers": headers or {}}
        self.calls.append(call)
        return self._handler(call)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """graph_fetch escribe en BD (graph_api_calls) y usage (graph_api_usage) --
    en tests unitarios se mockean ambos caminos."""
    calls_logged = []

    async def _fake_log_call(**kwargs):
        calls_logged.append(kwargs)

    async def _fake_record_usage(*a, **k):
        from core.gateway.meta_usage import UsageSnapshot

        return UsageSnapshot()

    monkeypatch.setattr(graph_mod, "_log_call", _fake_log_call)
    monkeypatch.setattr(graph_mod, "record_usage", _fake_record_usage)
    return calls_logged


def test_appsecret_proof_se_calcula_sobre_el_token_no_sobre_el_app_id():
    proof_a = appsecret_proof("token-A", APP_SECRET)
    proof_b = appsecret_proof("token-B", APP_SECRET)
    assert proof_a != proof_b

    # cambiar cualquier dato que NO sea el token no debería producir el mismo
    # proof que un app_id distinto usado como si fuera el token
    fake_app_id_como_token = appsecret_proof("999999", APP_SECRET)
    assert fake_app_id_como_token != proof_a


@pytest.mark.asyncio
async def test_url_nunca_contiene_access_token_como_query(monkeypatch, tmp_path):
    client = FakeAsyncClient(lambda call: FakeResponse(json_data={"ok": True}))
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", client)

    await graph_fetch(
        "/123456789", token=TOKEN, app_secret=APP_SECRET, tenant_id="t1", connection_id=None
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert "access_token" not in call["params"]
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "appsecret_proof" in call["params"]
    assert call["params"]["appsecret_proof"] == appsecret_proof(TOKEN, APP_SECRET)


@pytest.mark.asyncio
async def test_graph_api_call_registrado_sin_query_string(monkeypatch):
    logged = []

    async def _capture_log_call(**kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(graph_mod, "_log_call", _capture_log_call)
    client = FakeAsyncClient(lambda call: FakeResponse(json_data={"ok": True}))
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", client)

    await graph_fetch(
        "/123456789/subscriptions",
        token=TOKEN,
        app_secret=APP_SECRET,
        query={"fields": "a,b"},
        tenant_id="t1",
    )

    assert len(logged) == 1
    assert logged[0]["path"] == "/123456789/subscriptions"
    assert "?" not in logged[0]["path"]


@pytest.mark.asyncio
async def test_body_no_json_no_revienta(monkeypatch):
    def _handler(call):
        response = FakeResponse(status_code=429, text="<html>Too Many Requests</html>" * 50)
        response.json = _raise_json_error
        return response

    def _raise_json_error():
        raise ValueError("not json")

    client = FakeAsyncClient(_handler)
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", client)

    with pytest.raises(GraphError) as excinfo:
        await graph_fetch("/x", token=TOKEN, app_secret=APP_SECRET, tenant_id="t1")

    assert excinfo.value.http_status == 429
    assert len(str(excinfo.value)) < 700


@pytest.mark.asyncio
async def test_error_de_meta_se_propaga_clasificado(monkeypatch):
    client = FakeAsyncClient(
        lambda call: FakeResponse(status_code=401, json_data={"error": {"code": 190, "message": "bad token"}})
    )
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", client)

    with pytest.raises(GraphError) as excinfo:
        await graph_fetch("/x", token=TOKEN, app_secret=APP_SECRET, tenant_id="t1")

    assert excinfo.value.category == "AUTH"


@pytest.mark.asyncio
async def test_timeout_se_clasifica_transient(monkeypatch):
    import httpx as httpx_mod

    class _TimeoutClient(FakeAsyncClient):
        async def request(self, *a, **k):
            raise httpx_mod.TimeoutException("timeout")

    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", _TimeoutClient(lambda c: None))

    with pytest.raises(GraphError) as excinfo:
        await graph_fetch("/x", token=TOKEN, app_secret=APP_SECRET, tenant_id="t1")

    assert excinfo.value.category == "TRANSIENT"
    assert excinfo.value.is_transient is True


@pytest.mark.asyncio
async def test_version_sale_de_settings_si_no_se_pasa(monkeypatch):
    from core.config import get_settings

    client = FakeAsyncClient(lambda call: FakeResponse(json_data={"ok": True}))
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", client)

    await graph_fetch("/x", token=TOKEN, app_secret=APP_SECRET, tenant_id="t1")

    version = get_settings().graph_api_version
    assert client.calls[0]["url"] == f"https://graph.facebook.com/{version}/x"


@pytest.mark.asyncio
async def test_record_usage_se_llama_en_exito_y_en_error(monkeypatch):
    calls = []

    async def _capture_usage(*a, **k):
        calls.append(k.get("is_error"))
        from core.gateway.meta_usage import UsageSnapshot

        return UsageSnapshot()

    monkeypatch.setattr(graph_mod, "record_usage", _capture_usage)

    client_ok = FakeAsyncClient(lambda call: FakeResponse(json_data={"ok": True}))
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", client_ok)
    await graph_fetch("/x", token=TOKEN, app_secret=APP_SECRET, tenant_id="t1")

    client_err = FakeAsyncClient(
        lambda call: FakeResponse(status_code=400, json_data={"error": {"code": 100, "message": "bad"}})
    )
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", client_err)
    with pytest.raises(GraphError):
        await graph_fetch("/x", token=TOKEN, app_secret=APP_SECRET, tenant_id="t1")

    assert calls == [False, True]

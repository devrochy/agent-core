"""No fuga de tokens -- por construcción, no por filtro:
- El token viaja SOLO en Authorization: Bearer, nunca como ?access_token=.
- `graph_api_calls.path`/`_log_call` nunca incluyen query string.
- El allowlist de headers logueables descarta authorization/cookie.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

import core.gateway.meta_graph as graph_mod
from core.shared.redact import LOGGABLE_HEADERS, safe_headers

_FAKE_TOKEN = "EAA_TEST_TOKEN_QUE_NUNCA_DEBE_APARECER_EN_UN_LOG_1234567890"


def test_allowlist_de_headers_nunca_incluye_authorization_ni_cookie():
    assert "authorization" not in LOGGABLE_HEADERS
    assert "cookie" not in LOGGABLE_HEADERS


def test_safe_headers_descarta_todo_lo_no_listado():
    headers = httpx.Headers(
        {
            "Authorization": f"Bearer {_FAKE_TOKEN}",
            "Cookie": "session=abc123",
            "X-Business-Use-Case-Usage": '{"a":1}',
            "X-App-Usage": '{"b":2}',
            "X-Fb-Trace-Id": "trace-1",
        }
    )
    result = safe_headers(headers)
    assert _FAKE_TOKEN not in str(result)
    assert "session=abc123" not in str(result)
    assert set(result.keys()) == {"x-business-use-case-usage", "x-app-usage", "x-fb-trace-id"}


@pytest.mark.asyncio
async def test_graph_fetch_token_nunca_en_query_string_ni_en_path_logueado(monkeypatch):
    """El token va SIEMPRE en el header Authorization -- nunca en la URL ni
    en los params que `_log_call` termina persistiendo."""
    captured_request = {}
    captured_log_call = {}

    class FakeResponse:
        status_code = 200
        headers = httpx.Headers({})
        text = ""

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, params=None, json=None, headers=None):
            captured_request["url"] = url
            captured_request["params"] = params
            captured_request["headers"] = headers
            return FakeResponse()

    async def _fake_log_call(**kwargs):
        captured_log_call.update(kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(graph_mod, "_log_call", _fake_log_call)
    monkeypatch.setattr(graph_mod, "record_usage", lambda *a, **k: _noop())

    async def _noop():
        return None

    await graph_mod.graph_fetch(
        "/123456/subscriptions", token=_FAKE_TOKEN, app_secret="s3cr3t", version="v21.0",
        connection_id=uuid.uuid4(),
    )

    # El token nunca aparece en la URL construida.
    assert _FAKE_TOKEN not in captured_request["url"]
    # appsecret_proof (un HMAC, no el token) puede ir en params -- el token en sí no.
    assert _FAKE_TOKEN not in str(captured_request.get("params") or {})
    # El token SOLO va en el header Authorization.
    assert captured_request["headers"]["Authorization"] == f"Bearer {_FAKE_TOKEN}"
    # `_log_call` (que persiste en graph_api_calls) recibe el `path` SIN query string.
    assert captured_log_call["path"] == "/123456/subscriptions"
    assert "?" not in captured_log_call["path"]
    assert _FAKE_TOKEN not in str(captured_log_call)

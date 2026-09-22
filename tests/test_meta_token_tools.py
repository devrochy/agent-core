"""`meta_token_tools`: inspección y extensión de tokens para el wizard BYOK.

Se mockean `graph_fetch` (debug_token) y `httpx.AsyncClient` (oauth/access_token)
-- lo que se prueba es el parseo y el contrato, no la red.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

import core.gateway.meta_token_tools as tools


@pytest.mark.asyncio
async def test_inspect_token_permanente_no_expira(monkeypatch):
    async def _fake_graph_fetch(path, **kwargs):
        assert path == "/debug_token"
        return {
            "data": {
                "is_valid": True,
                "app_id": "123",
                "type": "SYSTEM_USER",
                "expires_at": 0,
                "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"],
            }
        }

    monkeypatch.setattr(tools, "graph_fetch", _fake_graph_fetch)

    info = await tools.inspect_token("123", "secret", "tok")
    assert info["is_valid"] is True
    assert info["never_expires"] is True
    assert info["has_whatsapp_scopes"] is True
    assert info["type"] == "SYSTEM_USER"


@pytest.mark.asyncio
async def test_inspect_token_temporal_reporta_expiracion(monkeypatch):
    async def _fake_graph_fetch(path, **kwargs):
        return {"data": {"is_valid": True, "expires_at": 1700000000, "scopes": ["public_profile"]}}

    monkeypatch.setattr(tools, "graph_fetch", _fake_graph_fetch)

    info = await tools.inspect_token("123", "secret", "tok")
    assert info["never_expires"] is False
    assert info["expires_at"] == 1700000000
    assert info["has_whatsapp_scopes"] is False


@pytest.mark.asyncio
async def test_extend_token_devuelve_token_long_lived(monkeypatch):
    class _Resp:
        status_code = 200
        headers: ClassVar[dict] = {}
        text = ""

        def json(self):
            return {"access_token": "long-token", "expires_in": 5184000}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **k: _Client())

    out = await tools.extend_token("123", "secret", "short-token")
    assert out["access_token"] == "long-token"
    assert out["expires_in"] == 5184000


@pytest.mark.asyncio
async def test_token_expiry_cero_es_none_y_unixtime_es_datetime(monkeypatch):
    from datetime import UTC, datetime

    import core.gateway.meta_byok as byok

    async def _never(app_id, app_secret, token):
        return {"expires_at": 0}

    monkeypatch.setattr(tools, "inspect_token", _never)
    assert await byok._token_expiry("a", "b", "c") is None

    async def _soon(app_id, app_secret, token):
        return {"expires_at": 1700000000}

    monkeypatch.setattr(tools, "inspect_token", _soon)
    assert await byok._token_expiry("a", "b", "c") == datetime.fromtimestamp(1700000000, UTC)

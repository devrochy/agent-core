"""`subscribe_page_webhook`: el parámetro es `subscribed_fields` (NUNCA
`fields`), usa el PAGE access token (no el de la app), y persiste los
fields REALES que Meta confirma por GET -- no los que se pidieron."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_messenger as messenger_mod


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeSession:
    def __init__(self, page):
        self._page = page

    async def execute(self, *a, **k):
        return _ScalarResult(self._page)


class FakeSessionCM:
    def __init__(self, page):
        self._page = page

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeSession(self._page)

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_post_usa_subscribed_fields_no_fields(monkeypatch):
    page = SimpleNamespace(subscribed_fields=[])
    monkeypatch.setattr(messenger_mod, "get_session", FakeSessionCM(page))
    monkeypatch.setattr(messenger_mod, "get_settings", lambda: SimpleNamespace(meta_app_secret="app-secret-no-usar"))

    calls = []

    async def _fake_graph_fetch(path, *, method="GET", token=None, query=None, **kwargs):
        calls.append({"path": path, "method": method, "token": token, "query": query})
        if method == "POST":
            return {"success": True}
        return {"data": [{"subscribed_fields": ["messages", "messaging_postbacks"]}]}

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    subscribed = await messenger_mod.subscribe_page_webhook("page-1", "tok-de-la-pagina", "v21.0", uuid.uuid4(), uuid.uuid4())

    assert subscribed is True
    post_call = calls[0]
    assert post_call["method"] == "POST"
    assert "subscribed_fields" in post_call["query"]
    assert "fields" not in post_call["query"] or post_call["query"].get("fields") is None
    assert post_call["token"] == "tok-de-la-pagina", "debe usar el PAGE token, no el de la app"
    assert page.subscribed_fields == ["messages", "messaging_postbacks"]


@pytest.mark.asyncio
async def test_persiste_los_fields_reales_no_los_pedidos(monkeypatch):
    """Si Meta confirma MENOS fields de los pedidos, se persiste lo real."""
    page = SimpleNamespace(subscribed_fields=[])
    monkeypatch.setattr(messenger_mod, "get_session", FakeSessionCM(page))
    monkeypatch.setattr(messenger_mod, "get_settings", lambda: SimpleNamespace(meta_app_secret="s"))

    async def _fake_graph_fetch(path, *, method="GET", **kwargs):
        if method == "POST":
            return {"success": True}
        return {"data": [{"subscribed_fields": ["messages"]}]}  # Meta solo confirmó "messages"

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    await messenger_mod.subscribe_page_webhook("page-1", "tok", "v21.0", uuid.uuid4(), uuid.uuid4())
    assert page.subscribed_fields == ["messages"]
    assert "messaging_postbacks" not in page.subscribed_fields

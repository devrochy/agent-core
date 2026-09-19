"""`list_phone_numbers`: pagina siguiendo `paging.cursors.after` hasta
agotar páginas -- `fields` siempre se pide explícito (sin él, Meta omite
`code_verification_status`/`platform_type`)."""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_phone_numbers as phone_numbers_mod


@pytest.mark.asyncio
async def test_pagina_hasta_agotar_after(monkeypatch):
    pages = [
        {
            "data": [{"id": "1"}, {"id": "2"}],
            "paging": {"cursors": {"after": "cursor-2"}, "next": "https://graph.example/next"},
        },
        {
            "data": [{"id": "3"}],
            "paging": {"cursors": {}},
        },
    ]
    calls = []

    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        calls.append(query)
        return pages.pop(0)

    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", _fake_graph_fetch)

    items = await phone_numbers_mod.list_phone_numbers("waba-1", "tok", "s3cr3t", "v21.0", uuid.uuid4())

    assert [item["id"] for item in items] == ["1", "2", "3"]
    assert calls[0]["fields"]  # siempre se pide fields explícito
    assert "after" not in calls[0]
    assert calls[1]["after"] == "cursor-2"


@pytest.mark.asyncio
async def test_sin_paginas_siguientes_no_hace_una_segunda_llamada(monkeypatch):
    calls = []

    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        calls.append(query)
        return {"data": [{"id": "solo-uno"}], "paging": {"cursors": {}}}

    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", _fake_graph_fetch)

    items = await phone_numbers_mod.list_phone_numbers("waba-1", "tok", "s3cr3t", "v21.0", uuid.uuid4())
    assert len(items) == 1
    assert len(calls) == 1

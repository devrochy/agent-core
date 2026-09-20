"""`discover_pages`: filtra por `tasks` -- ANALYZE/ADVERTISE no alcanzan
para mensajería, hace falta MANAGE o MODERATE. Nunca se guarda un token
inútil para una Página sin permiso suficiente."""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_messenger as messenger_mod


@pytest.mark.asyncio
async def test_pagina_sin_manage_o_moderate_queda_marcada_insuficiente(monkeypatch):
    async def _fake_graph_fetch(path, **kwargs):
        return {
            "data": [
                {"id": "p1", "name": "Solo analytics", "category": "Business", "access_token": "tok-p1", "tasks": ["ANALYZE"]},
                {"id": "p2", "name": "Con manage", "category": "Business", "access_token": "tok-p2", "tasks": ["MANAGE"]},
                {"id": "p3", "name": "Con moderate", "category": "Business", "access_token": "tok-p3", "tasks": ["MODERATE", "ADVERTISE"]},
            ]
        }

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    candidates = await messenger_mod.discover_pages("user-tok", "s3cr3t", "v21.0", uuid.uuid4())

    by_id = {c.page_id: c for c in candidates}
    assert by_id["p1"].insufficient_permission is True
    assert by_id["p1"].access_token is None
    assert "MANAGE" in by_id["p1"].reason or "MODERATE" in by_id["p1"].reason

    assert by_id["p2"].insufficient_permission is False
    assert by_id["p2"].access_token == "tok-p2"

    assert by_id["p3"].insufficient_permission is False
    assert by_id["p3"].access_token == "tok-p3"


@pytest.mark.asyncio
async def test_connect_page_rechaza_pagina_insuficiente(monkeypatch):
    candidate = messenger_mod.PageCandidate(
        page_id="p1", name="x", category=None, tasks=["ANALYZE"], access_token=None, insufficient_permission=True,
        reason="Falta MANAGE/MODERATE",
    )
    with pytest.raises(messenger_mod.MessengerError, match="MANAGE"):
        await messenger_mod.connect_page(uuid.uuid4(), uuid.uuid4(), candidate)

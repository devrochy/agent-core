"""`list_templates`: pagina siguiendo `paging.cursors.after` hasta agotar
páginas. `acquire_sync_lock`: bloquea una segunda llamada en la misma hora
(cron y botón manual comparten la llave). El upsert de `sync_templates`
(idempotencia real contra una BD) se cubre en
`tests/integration/test_meta_templates_admin.py`."""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_template_sync as sync_mod
from core.gateway.meta_template_sync import acquire_sync_lock, list_templates


@pytest.mark.asyncio
async def test_pagina_hasta_agotar_after(monkeypatch):
    pages = [
        {
            "data": [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}],
            "paging": {"cursors": {"after": "cursor-2"}, "next": "https://graph.example/next"},
        },
        {"data": [{"id": "3", "name": "c"}], "paging": {"cursors": {}}},
    ]
    calls = []

    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        calls.append(query)
        return pages.pop(0)

    monkeypatch.setattr(sync_mod, "graph_fetch", _fake_graph_fetch)

    items = await list_templates("waba-1", "tok", "secret", "v21.0", uuid.uuid4())

    assert [item["id"] for item in items] == ["1", "2", "3"]
    assert calls[0]["fields"]
    assert "after" not in calls[0]
    assert calls[1]["after"] == "cursor-2"


@pytest.mark.asyncio
async def test_acquire_sync_lock_bloquea_segunda_llamada_misma_hora(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.keys: dict[str, str] = {}

        async def set(self, key, value, ex=None, nx=False):
            if nx and key in self.keys:
                return None
            self.keys[key] = value
            return True

    fake = FakeRedis()
    monkeypatch.setattr(sync_mod, "get_redis", lambda: fake)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    assert await acquire_sync_lock(tenant_a, "waba-1") is True
    assert await acquire_sync_lock(tenant_a, "waba-1") is False
    assert await acquire_sync_lock(tenant_a, "waba-2") is True
    # `waba_id` no es único entre negocios: el turno de A no bloquea a B.
    assert await acquire_sync_lock(tenant_b, "waba-1") is True


def test_quality_score_de_graph_v2x_se_guarda_como_texto():
    """Graph v26 manda `quality_score` como objeto; la columna es texto. Con el
    objeto crudo el INSERT fallaba (asyncpg: expected str, got dict) y no se
    sincronizaba ninguna plantilla (visto en test el 2026-10-08)."""
    q = sync_mod._quality_score
    assert q({"quality_score": {"score": "UNKNOWN", "date": 1790240375}}) == "UNKNOWN"
    assert q({"quality_score": "GREEN"}) == "GREEN"
    assert q({"quality_score": {"date": 1}}) is None
    assert q({}) is None

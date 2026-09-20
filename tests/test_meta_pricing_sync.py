"""`sync_pricing_daily`: idempotente (correrlo dos veces con el mismo
data point no duplica, actualiza `cost`/`volume` en su lugar) -- probado
contra una fila fake en memoria, no una BD real (eso lo cubre
tests/integration/test_meta_costs_admin.py)."""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_pricing_analytics as pricing_mod
from core.gateway.meta_pricing_analytics import sync_pricing_daily


class FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeSession:
    def __init__(self, store):
        self._store = store

    async def execute(self, *a, **k):
        # Simula: la segunda pasada encuentra la fila ya insertada.
        return FakeResult(self._store.get("row"))

    def add(self, obj):
        self._store["row"] = obj

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSessionCM:
    def __init__(self, store):
        self._store = store

    def __call__(self):
        return FakeSession(self._store)


@pytest.mark.asyncio
async def test_sync_es_idempotente_actualiza_en_vez_de_duplicar(monkeypatch):
    async def _fake_fetch(*a, **k):
        return [
            pricing_mod.PricingAnalyticsPoint(
                start=1735689600, end=1735776000, phone_number="+54911", country="AR",
                tier="0:250000", pricing_type="regular", pricing_category="utility",
                volume=10, cost=1.0,
            )
        ]

    monkeypatch.setattr(pricing_mod, "fetch_pricing_analytics", _fake_fetch)

    store: dict = {}
    monkeypatch.setattr(pricing_mod, "get_session", FakeSessionCM(store))

    waba_account_id, tenant_id, connection_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    await sync_pricing_daily("waba-1", waba_account_id, tenant_id, 1735689600, 1735776000, "tok", "secret", "v21.0", connection_id)
    first_row = store["row"]
    assert first_row.volume == 10
    assert first_row.cost == 1.0

    # Segunda corrida: mismo día, cost/volume actualizado -- no debe crear una fila nueva.
    async def _fake_fetch_updated(*a, **k):
        return [
            pricing_mod.PricingAnalyticsPoint(
                start=1735689600, end=1735776000, phone_number="+54911", country="AR",
                tier="0:250000", pricing_type="regular", pricing_category="utility",
                volume=12, cost=1.44,
            )
        ]

    monkeypatch.setattr(pricing_mod, "fetch_pricing_analytics", _fake_fetch_updated)
    await sync_pricing_daily("waba-1", waba_account_id, tenant_id, 1735689600, 1735776000, "tok", "secret", "v21.0", connection_id)

    assert store["row"] is first_row  # misma fila, actualizada
    assert first_row.volume == 12
    assert first_row.cost == 1.44


@pytest.mark.asyncio
async def test_data_point_sin_start_se_omite(monkeypatch):
    async def _fake_fetch(*a, **k):
        return [pricing_mod.PricingAnalyticsPoint(volume=1, cost=0.1)]

    monkeypatch.setattr(pricing_mod, "fetch_pricing_analytics", _fake_fetch)
    store: dict = {}
    monkeypatch.setattr(pricing_mod, "get_session", FakeSessionCM(store))

    await sync_pricing_daily("waba-1", uuid.uuid4(), uuid.uuid4(), 1735689600, 1735776000, "tok", "secret", "v21.0", uuid.uuid4())
    assert "row" not in store

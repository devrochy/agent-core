"""`fetch_pricing_analytics`: arma el query de `fields` EXACTO (dimensions
sin corchetes, granularity DAILY); parsea con `extra="allow"` sin reventar
ante campos desconocidos."""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_pricing_analytics as pricing_mod
from core.gateway.meta_pricing_analytics import fetch_pricing_analytics


@pytest.mark.asyncio
async def test_arma_el_query_de_fields_exacto(monkeypatch):
    captured = {}

    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        captured["path"] = path
        captured["query"] = query
        return {"pricing_analytics": {"data": []}}

    monkeypatch.setattr(pricing_mod, "graph_fetch", _fake_graph_fetch)

    await fetch_pricing_analytics("waba-1", 1000, 2000, "tok", "secret", "v21.0", uuid.uuid4())

    assert captured["path"] == "/waba-1"
    fields = captured["query"]["fields"]
    assert fields == (
        "pricing_analytics.start(1000).end(2000).granularity(DAILY)"
        ".dimensions(PRICING_CATEGORY,PRICING_TYPE,TIER,COUNTRY)"
    )
    # Dimensions SIN corchetes -- confirmado explícitamente por el ticket.
    assert "[" not in fields and "]" not in fields


@pytest.mark.asyncio
async def test_parsea_con_extra_allow_sin_reventar(monkeypatch):
    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        return {
            "pricing_analytics": {
                "data": [
                    {
                        "start": 1000, "end": 2000, "phone_number": "+54911", "country": "AR",
                        "tier": "0:250000", "pricing_type": "regular", "pricing_category": "utility",
                        "volume": 10, "cost": 1.23, "campo_totalmente_nuevo_no_documentado": "x",
                    }
                ]
            }
        }

    monkeypatch.setattr(pricing_mod, "graph_fetch", _fake_graph_fetch)

    points = await fetch_pricing_analytics("waba-1", 1000, 2000, "tok", "secret", "v21.0", uuid.uuid4())
    assert len(points) == 1
    assert points[0].volume == 10
    assert points[0].cost == 1.23
    assert points[0].tier == "0:250000"


@pytest.mark.asyncio
async def test_lee_los_puntos_agrupados_en_data_points(monkeypatch):
    # Forma real de la respuesta de Meta (WABA de test, 2026-10-08).
    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        return {
            "pricing_analytics": {
                "data": [
                    {
                        "data_points": [
                            {"start": 1000, "end": 2000, "country": "CO", "pricing_type": "FREE_CUSTOMER_SERVICE",
                             "pricing_category": "SERVICE", "volume": 2, "cost": 0},
                            {"start": 1000, "end": 2000, "country": "CO", "pricing_type": "REGULAR",
                             "pricing_category": "UTILITY", "volume": 3, "cost": 0},
                        ]
                    }
                ]
            }
        }

    monkeypatch.setattr(pricing_mod, "graph_fetch", _fake_graph_fetch)

    points = await fetch_pricing_analytics("waba-1", 1000, 2000, "tok", "secret", "v21.0", uuid.uuid4())
    assert [(p.pricing_category, p.volume, p.cost) for p in points] == [("SERVICE", 2, 0), ("UTILITY", 3, 0)]
    assert all(p.country == "CO" and p.start == 1000 for p in points)


@pytest.mark.asyncio
async def test_data_point_invalido_se_omite_sin_reventar(monkeypatch):
    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        return {"pricing_analytics": {"data": [{"start": 1000}]}}  # sin volume/cost obligatorios

    monkeypatch.setattr(pricing_mod, "graph_fetch", _fake_graph_fetch)

    points = await fetch_pricing_analytics("waba-1", 1000, 2000, "tok", "secret", "v21.0", uuid.uuid4())
    assert points == []


@pytest.mark.asyncio
async def test_sin_pricing_analytics_en_la_respuesta_devuelve_vacio(monkeypatch):
    async def _fake_graph_fetch(path, *, query=None, **kwargs):
        return {}

    monkeypatch.setattr(pricing_mod, "graph_fetch", _fake_graph_fetch)

    points = await fetch_pricing_analytics("waba-1", 1000, 2000, "tok", "secret", "v21.0", uuid.uuid4())
    assert points == []

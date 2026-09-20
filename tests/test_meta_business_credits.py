"""`get_extended_credits`: solo lectura -- el módulo no expone ninguna
función de escritura (Meta no soporta create/update/delete)."""

from __future__ import annotations

import inspect
import uuid

import pytest

import core.gateway.meta_business_credits as credits_mod
from core.gateway.meta_business_credits import get_extended_credits


def test_modulo_no_expone_funciones_de_escritura():
    public_functions = [
        name for name, obj in vars(credits_mod).items()
        if inspect.isfunction(obj) and not name.startswith("_") and obj.__module__ == credits_mod.__name__
    ]
    assert public_functions == ["get_extended_credits"]


@pytest.mark.asyncio
async def test_get_extended_credits_hace_un_get_al_business_id(monkeypatch):
    captured = {}

    async def _fake_graph_fetch(path, **kwargs):
        captured["path"] = path
        captured["method"] = kwargs.get("method", "GET")
        return {"data": [{"id": "credit-1", "legal_entity_name": "ACME"}]}

    monkeypatch.setattr(credits_mod, "graph_fetch", _fake_graph_fetch)

    result = await get_extended_credits("biz-123", "tok", "secret", "v21.0", uuid.uuid4())

    assert captured["path"] == "/biz-123/extendedcredits"
    assert captured["method"] == "GET"
    assert result == [{"id": "credit-1", "legal_entity_name": "ACME"}]


@pytest.mark.asyncio
async def test_sin_data_en_la_respuesta_devuelve_lista_vacia(monkeypatch):
    async def _fake_graph_fetch(path, **kwargs):
        return {}

    monkeypatch.setattr(credits_mod, "graph_fetch", _fake_graph_fetch)

    result = await get_extended_credits("biz-123", "tok", "secret", "v21.0", uuid.uuid4())
    assert result == []

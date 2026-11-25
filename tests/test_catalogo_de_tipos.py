"""HU-AOS-94 · lógica pura del catálogo de tipos (ADR-0207 §6): tipo del agente, transversales admitidas y packs ajenos."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.agents import catalogo_de_tipos as cat
from core.mcp.types import AgentTypeManifest


def _tipo(**extra: object) -> AgentTypeManifest:
    base = {
        "key": "ventas",
        "habilidades_obligatorias": ["core:recepcion", "pack_commerce"],
        "habilidades_transversales": ["pack_payments"],
    }
    return AgentTypeManifest.model_construct(**{**base, **extra})  # type: ignore[arg-type]


@pytest.fixture
def con_tipo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cat, "tipos_de_agente", lambda: {"ventas": _tipo()})


def test_tipo_por_source_template_y_luego_por_key(con_tipo: None) -> None:
    por_plantilla = cat.tipo_del_agente(SimpleNamespace(source_template="ventas", key="otra"))
    por_key = cat.tipo_del_agente(SimpleNamespace(source_template=None, key="ventas"))
    assert por_plantilla is not None and por_plantilla.key == "ventas"
    assert por_key is not None and por_key.key == "ventas"
    assert cat.tipo_del_agente(SimpleNamespace(source_template=None, key="propia")) is None


def test_transversales_admitidas(con_tipo: None) -> None:
    assert cat.transversales_admitidas(SimpleNamespace(source_template="ventas", key="x")) == {"pack_payments"}
    assert cat.transversales_admitidas(SimpleNamespace(source_template=None, key="propia")) is None


def test_packs_de_especialidad_ajenos(con_tipo: None) -> None:
    agente = SimpleNamespace(source_template="ventas", key="x")
    assert cat.packs_de_especialidad_ajenos(agente, {"pack_commerce", "pack_calendar"}) == {"pack_calendar"}
    sin_tipo = SimpleNamespace(source_template=None, key="propia")
    assert cat.packs_de_especialidad_ajenos(sin_tipo, {"pack_commerce"}) == {"pack_commerce"}

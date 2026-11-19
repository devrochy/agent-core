"""Resumen por agente (ADR-0171): variación contra el período anterior, ventanas y registro de módulos.

Sin base de datos: la parte pura. El recorrido con datos y permisos está en
`tests/integration/test_resumen_agentes_api.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.admin.resumen_registry import (
    Destino,
    Kpi,
    PeriodoResumen,
    ResumenProvider,
    ResumenRegistry,
)
from core.metricas.resumen_agentes import _kpi_a_dict, periodo


@pytest.mark.parametrize(
    ("valor", "previo", "esperado"),
    [
        (30.0, 10.0, 200.0),  # sube
        (5.0, 10.0, -50.0),  # baja
        (10.0, 10.0, 0.0),  # igual
        (7.0, 0.0, None),  # previo en cero: no se inventa un porcentaje infinito
        (7.0, None, None),  # foto de hoy, sin comparación
        (None, 10.0, None),  # sin datos suficientes
    ],
)
def test_delta_pct(valor, previo, esperado) -> None:
    assert Kpi("k", "K", valor, previo=previo).delta_pct == esperado


def test_delta_con_previo_negativo_usa_el_modulo() -> None:
    assert Kpi("k", "K", -5.0, previo=-10.0).delta_pct == 50.0


def test_periodo_son_dos_ventanas_contiguas_de_7_dias() -> None:
    ahora = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    p = periodo(ahora)
    assert p.hasta == ahora
    assert p.hasta - p.desde == timedelta(days=7)
    assert p.previo_hasta == p.desde  # sin hueco ni solape
    assert p.previo_hasta - p.previo_desde == timedelta(days=7)


def test_versus_dice_contra_que_se_compara() -> None:
    assert _kpi_a_dict(Kpi("a", "A", 1.0, previo=1.0))["versus"] == "los 7 días anteriores"
    assert (
        _kpi_a_dict(Kpi("a", "A", 1.0, previo=1.0, versus="el mes anterior"))["versus"]
        == "el mes anterior"
    )
    assert _kpi_a_dict(Kpi("a", "A", 1.0))["versus"] is None  # foto de hoy: nada que comparar


def test_kpi_a_dict_lleva_el_delta_y_el_destino_semantico() -> None:
    k = Kpi(
        "pedidos",
        "Pedidos",
        12.0,
        previo=10.0,
        polaridad="up-good",
        destino=Destino("comercio.pedidos", {"estado": "abierto"}),
    )
    d = _kpi_a_dict(k)
    assert d["delta_pct"] == pytest.approx(20.0)
    assert d["destino"] == {"ruta": "comercio.pedidos", "params": {"estado": "abierto"}}
    assert _kpi_a_dict(Kpi("x", "X", None))["destino"] is None


async def _vacio(_tid, _p: PeriodoResumen) -> list[Kpi]:
    return []


def test_registro_es_idempotente_y_filtra_por_packs_habilitados() -> None:
    reg = ResumenRegistry()
    a = ResumenProvider("pack_a", "ventas", "A", "pack_a", _vacio)
    reg.register(a)
    reg.register(ResumenProvider("pack_a", "ventas", "A2", "pack_a", _vacio))  # reemplaza
    reg.register(ResumenProvider("pack_b", "agenda", "B", "pack_b", _vacio))
    assert [p.nombre for p in reg.providers_for(None)] == ["A2", "B"]
    assert [p.plugin_id for p in reg.providers_for({"pack_b"})] == ["pack_b"]
    assert reg.providers_for(set()) == []

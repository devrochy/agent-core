"""HU-AOS-52 (ADR-0171 §5): el recorte del General al publicar un especialista. Puro, con el catálogo REAL."""

from __future__ import annotations

from typing import Any

import pytest

from core.agents.coordinacion import recortar_general
from core.presets import load_plantilla

VENTAS = ["pack_commerce.tomar_pedido", "pack_commerce.contratar_plan"]


@pytest.fixture(scope="module")
def catalogo() -> Any:
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    return catalogo_de_acciones(plugin_manager.scan())


def _general(catalogo: Any) -> dict[str, Any]:
    return {
        "model": {"primary": "modelo-x"},
        "playbooks": [{"name": "ventas"}, {"name": "citas"}, {"name": "general"}],
        "procedures": [
            {"id": "pack_commerce.tomar_pedido", "version": "^1.0"},
            {"id": "pack_commerce.contratar_plan", "version": "^1.0"},
            {"id": "pack_calendar.agendar_cita", "version": "^1.0"},
            {"id": "pack_calendar.gestion_citas", "version": "^1.0"},
            {"id": "pack_commerce.descubrimiento", "version": "^1.0"},
            {"id": "core.saludo", "version": "^1.0"},
        ],
        "tools": [{"tool": n} for n in sorted(catalogo)],
        "guardrails": [{"kind": "required_tool", "tool": "crear_pedido"}, {"kind": "required_tool", "tool": "escalate_to_human"}],
    }


def test_con_todos_los_superpoderes_del_pack_se_van_sus_tools_playbooks_y_guardrails(catalogo: Any) -> None:
    nuevo, quitado = recortar_general(
        _general(catalogo), procedimientos=set(VENTAS), plantilla=load_plantilla("ventas"), catalogo=catalogo
    )
    assert quitado["procedures"] == sorted([*VENTAS, "pack_commerce.descubrimiento"])
    tools = {b["tool"] for b in nuevo["tools"]}
    assert not any(catalogo[n].owner == "pack_commerce" for n in tools)
    # Lo de otros packs y las esenciales del core siguen; no se toca lo ajeno.
    assert {"escalate_to_human", "check_availability", "remember_fact"} <= tools
    assert [p["name"] for p in nuevo["playbooks"]] == ["citas", "general"]
    assert nuevo["guardrails"] == [{"kind": "required_tool", "tool": "escalate_to_human"}]
    # Cubierto Comercio, también se van sus procedimientos que no son superpoder; lo de otros packs y el core quedan.
    assert [r["id"] for r in nuevo["procedures"]] == [
        "pack_calendar.agendar_cita",
        "pack_calendar.gestion_citas",
        "core.saludo",
    ]


def test_si_al_general_le_queda_un_superpoder_del_pack_conserva_las_tools(catalogo: Any) -> None:
    """Un especialista de Ventas «solo planes» no deja al General sin la caja de pedidos."""
    nuevo, quitado = recortar_general(
        _general(catalogo),
        procedimientos={"pack_commerce.contratar_plan"},
        plantilla=load_plantilla("ventas"),
        catalogo=catalogo,
    )
    # Le queda `tomar_pedido`: Comercio no está cubierto y el General conserva sus tools y sus otros procedimientos.
    assert quitado == {"procedures": ["pack_commerce.contratar_plan"]}
    assert {"crear_pedido", "generar_cotizacion"} <= {b["tool"] for b in nuevo["tools"]}
    assert [p["name"] for p in nuevo["playbooks"]] == ["ventas", "citas", "general"]


def test_suscripciones_se_lleva_las_sub_del_core(catalogo: Any) -> None:
    general = _general(catalogo)
    general["procedures"].append({"id": "pack_subscriptions.cambiar_plan", "version": "^1.0"})
    nuevo, quitado = recortar_general(
        general,
        procedimientos={"pack_subscriptions.cambiar_plan"},
        plantilla=load_plantilla("suscripciones"),
        catalogo=catalogo,
    )
    assert "sub_consultar" in quitado["tools"]
    assert not any(b["tool"].startswith("sub_") for b in nuevo["tools"])
    assert any(b["tool"] == "crear_pedido" for b in nuevo["tools"])


def test_es_idempotente_y_no_muta_la_entrada(catalogo: Any) -> None:
    original = _general(catalogo)
    antes = repr(original)
    una, _ = recortar_general(original, procedimientos=set(VENTAS), plantilla=load_plantilla("ventas"), catalogo=catalogo)
    assert repr(original) == antes
    otra, quitado = recortar_general(una, procedimientos=set(VENTAS), plantilla=load_plantilla("ventas"), catalogo=catalogo)
    assert otra == una and quitado == {}

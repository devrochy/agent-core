"""HU-ARCH-34: qué cuenta como faltante en el reporte de acciones sin conceder (sin base)."""

from __future__ import annotations

from core.releases.reporte_bindings import (
    MOTIVO_CORE,
    MOTIVO_FLUJO,
    ReporteBindings,
    core_esperadas,
    declaradas_con_binding,
    sin_binding,
)


def test_binding_deshabilitado_y_declarativa_cuentan_como_concedidas() -> None:
    contenido = {
        "tools": [{"tool": "a", "enabled": True}, {"tool": "b", "enabled": False}],
        "declarativeTools": [{"name": "c"}],
    }
    assert declaradas_con_binding(contenido) == {"a", "b", "c"}


def test_las_sub_no_son_esperadas_en_todo_agente() -> None:
    assert core_esperadas({"escalate_to_human", "sub_consultar"}) == {"escalate_to_human"}


def test_faltante_del_core_en_cualquier_agente() -> None:
    faltantes, fuera = sin_binding({"a"}, del_core={"a", "b"}, del_flujo=(), es_por_defecto=False)
    assert faltantes == {"b": MOTIVO_CORE} and fuera == set()


def test_flujo_es_faltante_solo_en_el_agente_por_defecto() -> None:
    assert sin_binding(set(), del_core=(), del_flujo=["x"], es_por_defecto=True) == ({"x": MOTIVO_FLUJO}, set())
    assert sin_binding(set(), del_core=(), del_flujo=["x"], es_por_defecto=False) == ({}, {"x"})


def test_un_reporte_con_errores_no_vale_como_limpio() -> None:
    assert ReporteBindings().limpio
    assert not ReporteBindings(errores=["negocio: OSError"]).limpio

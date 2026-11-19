"""Cálculo del veredicto del gate de pass^k (HU-AOS-21, adenda de ADR-0117). Puro."""

from __future__ import annotations

from core.releases.veredicto import Escenario, Medicion, calcular


def _m(pid: str, *pass_k: float, sha: str = "s1") -> Medicion:
    return Medicion(pid, tuple(Escenario(f"{pid}-{i}", 4, p, p, p >= 1.0) for i, p in enumerate(pass_k)), sha)


def test_pass_k_del_procedimiento_es_la_fraccion_de_escenarios_estables() -> None:
    assert _m("a", 1.0, 1.0).pass_k == 1.0
    assert _m("a", 1.0, 0.0).pass_k == 0.5
    assert _m("a").pass_k == 0.0


def test_aprueba_con_todo_en_el_umbral_y_rechaza_si_uno_cae() -> None:
    assert calcular([_m("a", 1.0, 1.0), _m("b", 1.0, 1.0)]).aprobado
    v = calcular([_m("a", 1.0, 1.0), _m("b", 1.0, 0.0)])
    assert not v.aprobado and v.motivo == "bajo_umbral_o_regresion:b"
    assert v.por_procedimiento["b"] == {
        "pass_k": 0.5,
        "activa": None,
        "ok": False,
        "escenarios": 2,
        "escenarios_sha256": "s1",
    }


def test_no_puede_bajar_frente_a_la_activa_con_los_mismos_escenarios() -> None:
    activa = {"a": {"pass_k": 1.0, "escenarios_sha256": "s1"}}
    # 7 de 8 = 0,875 ≥ 0,85 pero menor que la activa (1,0): rechazado.
    ocho = _m("a", *([1.0] * 7 + [0.0]))
    assert not calcular([ocho], activa).aprobado
    # Con otros escenarios (otro sha) la activa no se compara: solo el umbral.
    otros = Medicion("a", ocho.escenarios, "s2")
    v = calcular([otros], activa)
    assert v.aprobado and v.por_procedimiento["a"]["activa"] is None


def test_sin_mediciones_no_aprueba() -> None:
    v = calcular([])
    assert not v.aprobado and v.motivo == "sin_mediciones"

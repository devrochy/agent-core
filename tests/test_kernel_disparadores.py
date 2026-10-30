"""HU-AOS-17 · puntaje léxico de disparadores contra el golden de ruteo (sin LLM ni embeddings).

El golden (`tests/fixtures/ruteo/golden.jsonl`, ≥ 60 casos) usa los ejemplos que publican hoy los
packs. Por catálogo (los procedimientos que tendría una release) se puntúa cada texto y la ruta es
el candidato fuerte con los umbrales por defecto del KernelSpec (0,80 y margen 0,15). La meta de
R3 (design §8): precisión ≥ 0,90 y recall ≥ 0,85 por procedimiento.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.kernel.disparadores import (
    Exponente,
    Indice,
    exponentes_de,
    fuerte,
    parsear_lexico,
    terminos,
)
from core.kernel.tarjeta import RotulosInvalidos

RAIZ = Path(__file__).resolve().parents[1]
GOLDEN = RAIZ / "tests" / "fixtures" / "ruteo" / "golden.jsonl"
PLAN = "pack_subscriptions.contratar_plan"
CAMBIO = "pack_subscriptions.cambiar_plan"
CITA = "pack_calendar.agendar_cita"
PROMESA = "pack_cobranza.promesa_de_pago"
PEDIDO = "pack_commerce.tomar_pedido"
CATALOGOS = {"planes": [PLAN, CAMBIO, CITA, PROMESA], "tienda": [PEDIDO, CITA, PROMESA]}


def _procedimientos() -> dict[str, Any]:
    salida: dict[str, Any] = {}
    for archivo in sorted(RAIZ.glob("packages/pack-*/procedures/*.json")):
        crudo = json.loads(archivo.read_text(encoding="utf-8"))
        trig = crudo.get("triggers") or {}
        salida[crudo["id"]] = SimpleNamespace(
            triggers=SimpleNamespace(examples=trig.get("examples") or [], description=trig.get("description") or "")
        )
    return salida


def _casos() -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in GOLDEN.read_text(encoding="utf-8").splitlines() if linea.strip()]


def _indices() -> dict[str, Indice]:
    procs = _procedimientos()
    return {c: Indice(exponentes_de({p: procs[p] for p in ps})) for c, ps in CATALOGOS.items()}


def test_el_golden_tiene_al_menos_60_casos_y_procedimientos_que_existen() -> None:
    casos = _casos()
    assert len(casos) >= 60
    procs = _procedimientos()
    for caso in casos:
        assert caso["catalogo"] in CATALOGOS
        assert caso["esperado"] is None or caso["esperado"] in procs
        assert caso["esperado"] is None or caso["esperado"] in CATALOGOS[caso["catalogo"]]


# Medido hoy (2026-10-18), con los ejemplos que publican los packs y sin overrides de agentes:
# precisión 1,00 en los cinco; recall 0,93 (agendar_cita), 1,00 (promesa_de_pago, cambiar_plan),
# 0,90 (tomar_pedido) y 0,85 (contratar_plan: 11/13, falla con nombres de plan que ningún ejemplo
# trae, "el plan empresarial", y "cómo pago el plan anual"). La meta de R3 es recall ≥ 0,85 por
# procedimiento; el piso de este test es 0,80 para no ajustar el golden al léxico. Se sube con más
# ejemplos en el pack o en `procedures[].overrides.examples` del agente (ADR-0138).
# HU-AOS-22 (2026-10-19) suma las tres frases del dogfooding de solvershub: contratar_plan queda en
# 12/14 (0,86) y cambiar_plan en 11/11. «cuánto cuesta el plan basic» no tiene procedimiento aquí,
# pero con el catálogo real de solvershub (con tomar_pedido y sin promesa_de_pago) el IDF cambia y
# va a cambiar_plan: se corrige con ejemplos propios de Ventas desde el editor (runbook de
# solvershub-comercial) y lo cubre `test_dogfooding_solvershub_ventas_agenda_y_suscripciones`.
PISO_RECALL = 0.80


def test_precision_y_recall_por_procedimiento_con_los_umbrales_por_defecto() -> None:
    indices = _indices()
    aciertos: dict[str, int] = {}
    predichos: dict[str, int] = {}
    reales: dict[str, int] = {}
    for caso in _casos():
        ruta = fuerte(indices[caso["catalogo"]].puntuar(caso["texto"]), strong=0.80, margin=0.15)
        predicho = ruta.procedure_id if ruta else None
        esperado = caso["esperado"]
        if predicho:
            predichos[predicho] = predichos.get(predicho, 0) + 1
        if esperado:
            reales[esperado] = reales.get(esperado, 0) + 1
        if predicho and predicho == esperado:
            aciertos[predicho] = aciertos.get(predicho, 0) + 1
    for pid in sorted(reales):
        precision = aciertos.get(pid, 0) / predichos[pid] if predichos.get(pid) else 1.0
        recall = aciertos.get(pid, 0) / reales[pid]
        assert precision >= 0.90, (pid, precision)
        assert recall >= PISO_RECALL, (pid, recall)
    total = sum(aciertos.values()) / sum(reales.values())
    assert total >= 0.90, total


def test_agendar_una_demo_sin_tarea_en_foco_tiene_confianza_alta() -> None:
    puntajes = _indices()["planes"].puntuar("quiero agendar una demo")
    assert puntajes[0].procedure_id == CITA
    assert puntajes[0].score >= 0.80
    assert fuerte(puntajes, strong=0.80, margin=0.15) is not None


def test_un_saludo_o_un_texto_vacio_no_tiene_candidatos() -> None:
    indice = _indices()["planes"]
    assert indice.puntuar("hola, buenas tardes") == []
    assert indice.puntuar("") == []
    assert fuerte([], strong=0.8, margin=0.15) is None


def test_un_empate_no_es_fuerte() -> None:
    indice = Indice([Exponente("a.uno", ("quiero una cita",)), Exponente("b.dos", ("quiero una cita",))])
    puntajes = indice.puntuar("quiero una cita")
    assert len(puntajes) == 2 and puntajes[0].score == puntajes[1].score
    assert fuerte(puntajes, strong=0.8, margin=0.15) is None


def test_los_ejemplos_del_agente_suman() -> None:
    procs = _procedimientos()
    sin = Indice(exponentes_de({CITA: procs[CITA]})).puntuar("me interesa conocer la plataforma con un experto")
    con = Indice(
        exponentes_de({CITA: procs[CITA]}, {CITA: ["quiero conocer la plataforma con un experto"]})
    ).puntuar("me interesa conocer la plataforma con un experto")
    assert (con[0].score if con else 0) > (sin[0].score if sin else 0)


def test_terminos_normaliza_equivalencias_y_frases() -> None:
    assert terminos("¡Quiero AGÉNDAR una Demo!") == terminos("reservar una reunión")
    assert "aldia" in terminos("la otra semana me pongo al día")


def test_el_lexico_roto_falla_cerrado() -> None:
    with pytest.raises(RotulosInvalidos):
        parsear_lexico("## equivalencias\na: b\n")
    with pytest.raises(RotulosInvalidos):
        parsear_lexico("## vacias\nel\n")
    with pytest.raises(RotulosInvalidos):
        parsear_lexico("## vacias\nel\n## equivalencias\nsin dos puntos\n")


def test_el_texto_largo_se_recorta() -> None:
    indice = _indices()["planes"]
    assert indice.puntuar("x " * 10_000 + "quiero agendar una demo") == []

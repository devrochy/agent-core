"""Escenarios mínimos por procedimiento para el gate de pass^k (HU-AOS-21, adenda de ADR-0117).

Cada journey declara `procedimientos: [id]`. Un procedimiento publicado por un pack necesita al
menos 2 escenarios; los que todavía no los tienen están en `PENDIENTES`, una lista que solo se
achica: el test falla si uno de la lista llega a 2 y sigue listado. Escribir escenarios que nunca
corrieron no demuestra nada, y grabarlos es LLM real con OK del dueño.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
MINIMO = 2
# Procedimiento → escenarios que tiene hoy (2026-10-30). `pack_subscriptions.contratar_plan` ya tiene 2
# (los journeys 01 y 02 de `procedimientos/`, que antes declaraban el de commerce: SPEC-43, HU-COM-17);
# sus pasos de verificación se ajustan al regrabarlos con LLM real y OK del dueño.
PENDIENTES = {
    "pack_subscriptions.cambiar_plan": 0,
    "pack_cobranza.promesa_de_pago": 0,
    "pack_commerce.tomar_pedido": 1,
    "pack_calendar.agendar_cita": 1,
}


def _procedimientos_publicados() -> set[str]:
    return {
        json.loads(p.read_text(encoding="utf-8"))["id"]
        for p in RAIZ.glob("packages/pack-*/procedures/*.json")
    }


def _escenarios() -> Counter[str]:
    cuenta: Counter[str] = Counter()
    for archivo in sorted(RAIZ.glob("evals/journeys/**/*.json")):
        datos = json.loads(archivo.read_text(encoding="utf-8"))
        for pid in datos.get("procedimientos") or []:
            cuenta[str(pid)] += 1
    return cuenta


def test_escenarios_declaran_procedimientos_que_existen() -> None:
    desconocidos = set(_escenarios()) - _procedimientos_publicados()
    assert not desconocidos, f"escenarios con procedimientos que ningún pack publica: {sorted(desconocidos)}"


def test_cada_procedimiento_publicado_tiene_sus_escenarios_minimos() -> None:
    cuenta = _escenarios()
    faltan = {
        pid: cuenta[pid]
        for pid in sorted(_procedimientos_publicados())
        if pid not in PENDIENTES and cuenta[pid] < MINIMO
    }
    assert not faltan, f"procedimientos con menos de {MINIMO} escenarios (agrega escenarios o justifícalo): {faltan}"


def test_la_lista_de_pendientes_solo_se_achica() -> None:
    cuenta = _escenarios()
    publicados = _procedimientos_publicados()
    assert set(PENDIENTES) <= publicados, f"pendientes que ya no existen: {sorted(set(PENDIENTES) - publicados)}"
    resueltos = sorted(pid for pid in PENDIENTES if cuenta[pid] >= MINIMO)
    assert not resueltos, f"ya tienen {MINIMO} escenarios: sácalos de PENDIENTES: {resueltos}"
    desactualizados = {pid: (n, cuenta[pid]) for pid, n in PENDIENTES.items() if cuenta[pid] != n}
    assert not desactualizados, f"actualiza el conteo de PENDIENTES (antes, ahora): {desactualizados}"

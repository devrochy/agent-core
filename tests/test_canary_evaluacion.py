"""HU-AOS-32 · la evaluación del canary (pura): muestra, no-inferioridad y señal dura."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.releases.canary_evaluacion import UMBRALES, Carril, decidir, senal_dura, umbrales

BASE = Carril(conversaciones=300, conversaciones_escaladas=30, tareas_terminadas=150, tareas_completadas=105, costo_usd=6.0, p95_ms=3000)


def test_sin_muestra_si_falta_una_de_las_dos() -> None:
    poco = Carril(conversaciones=199, conversaciones_escaladas=10)
    assert decidir(poco, BASE, horas=100).estado == "sin_muestra"
    mucho = Carril(conversaciones=500, conversaciones_escaladas=10)
    assert decidir(mucho, BASE, horas=71.9).estado == "sin_muestra"  # 200 y además 72 h


def test_peor_por_escalamiento_o_completitud() -> None:
    escala = Carril(conversaciones=200, conversaciones_escaladas=25, tareas_terminadas=120, tareas_completadas=84)
    v = decidir(escala, BASE, horas=80)
    assert v.estado == "peor" and v.motivos == ["escalamiento"]  # 12,5 % frente a 10 %
    completa = Carril(conversaciones=200, conversaciones_escaladas=20, tareas_terminadas=120, tareas_completadas=78)
    assert decidir(completa, BASE, horas=80).motivos == ["completitud"]  # 65 % frente a 70 %


def test_completitud_solo_con_cien_tareas_por_carril() -> None:
    pocas = Carril(conversaciones=200, conversaciones_escaladas=20, tareas_terminadas=40, tareas_completadas=10)
    assert decidir(pocas, BASE, horas=80).estado == "listo"


def test_costo_y_lentitud_solo_avisan() -> None:
    caro = Carril(conversaciones=200, conversaciones_escaladas=20, costo_usd=6.0, p95_ms=4500)
    v = decidir(caro, BASE, horas=80)
    assert v.estado == "listo" and v.advertencias == ["costo", "lentitud"]


def test_umbrales_congelados_en_el_evento() -> None:
    u = umbrales({"muestra_conversaciones": 50, "desconocido": 1})
    assert u["muestra_conversaciones"] == 50 and "desconocido" not in u and u["margen_pp"] == UMBRALES["margen_pp"]
    assert decidir(Carril(conversaciones=60, conversaciones_escaladas=6), BASE, horas=80, u=u).estado == "listo"


def _conv(mal: bool = False, accion: bool = False, sin: bool = False, horas: float = 0) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=uuid.uuid4(), ultimo=datetime.now(UTC) - timedelta(hours=horas),
        llego_mal=mal, accion_fallida=accion, sin_resultado=sin,
    )


def test_senal_dura() -> None:
    sanas = [_conv() for _ in range(40)]
    malas = [_conv(mal=True) for _ in range(6)] + [_conv() for _ in range(14)]
    # Menos de 20 con resultado: no decide.
    assert not senal_dura([_conv(mal=True)] * 5, sanas).activa
    # 30 % frente a 0 % del estable: se activa.
    s = senal_dura(malas, sanas)
    assert s.activa and s.llegaron_mal == 6 and s.tasa == 0.3 and s.tasa_estable == 0.0
    # El estable también falla al 20 %: 30 % no llega al doble.
    estable_mal = [_conv(mal=True) for _ in range(8)] + [_conv() for _ in range(32)]
    assert not senal_dura(malas, estable_mal).activa
    # Sin estable comparable, los `action_failed` no cuentan (una caída de la pasarela).
    acciones = [_conv(accion=True) for _ in range(10)] + [_conv() for _ in range(10)]
    assert not senal_dura(acciones, []).activa
    assert senal_dura(acciones, sanas).activa
    # Las que no tienen resultado salen del denominador.
    con_huecos = [_conv(sin=True) for _ in range(5)] + malas
    assert senal_dura(con_huecos, sanas).sin_resultado == 5

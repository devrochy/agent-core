"""HU-AOS-20 parte 1 · detección de varios pedidos en un mensaje y la pila en sombra (sin LLM).

- **Segmentar:** corta por conectores y signos sin romper «1.500» ni «p. m.»; las partes sin
  contenido se unen; tope de segmentos.
- **Detectar:** contra `tests/fixtures/ruteo/golden_pila.jsonl` (dos pedidos y negativos con «y»),
  con precisión 1,00 (nunca abre lo que no se pidió) y aciertos exactos ≥ 0,85.
- **Plan:** qué va en foco, en pausa y qué sobra según `maxStack` y si hay foco.
- **Banderas:** valores desconocidos → apagado; `shadow_route.pila` se suma sin pisar la ruta.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_kernel_disparadores import _indices

from core.kernel.banderas import Colaboracion
from core.kernel.disparadores import MAX_SEGMENTOS, parsear_lexico, segmentar
from core.kernel.router import intenciones_del_texto, plan_de_pila
from core.kernel.ruteo_sombra import metricas_de_pila
from core.kernel.tarjeta import RotulosInvalidos

GOLDEN_PILA = Path(__file__).resolve().parent / "fixtures" / "ruteo" / "golden_pila.jsonl"
PLAN = "pack_commerce.contratar_plan"
CITA = "pack_calendar.agendar_cita"


def _casos() -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in GOLDEN_PILA.read_text(encoding="utf-8").splitlines() if linea.strip()]


# ---------------------------------------------------------------- segmentar
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("quiero el Premium y una demo el jueves", ["quiero el Premium", "una demo el jueves"]),
        ("me cobraron 1.500 y quiero pagar", ["me cobraron 1.500", "quiero pagar"]),
        ("agendar a las 3 p. m. por favor", ["agendar a las 3 p. m. por favor"]),
        ("Quiero el plan. Y una demo", ["Quiero el plan", "una demo"]),
        ("hola y buenas, quiero una cita", ["hola buenas quiero una cita"]),
        ("contratar el plan, además de paso una demo", ["contratar el plan", "una demo"]),
    ],
)
def test_segmentar(texto: str, esperado: list[str]) -> None:
    assert segmentar(texto) == esperado


def test_segmentar_tiene_tope() -> None:
    texto = ", ".join(f"pedido{i} cosa" for i in range(20))
    partes = segmentar(texto)
    assert len(partes) == MAX_SEGMENTOS and "pedido19" in partes[-1]


def test_el_lexico_exige_conectores() -> None:
    with pytest.raises(RotulosInvalidos, match="conectores"):
        parsear_lexico("## vacias\nel\n## equivalencias\na: b\n")
    lx = parsear_lexico("## vacias\nel\n## equivalencias\na: b\n## conectores\ny\nde paso\n")
    assert lx.conectores == (("de", "paso"), ("y",))


# ---------------------------------------------------------------- detectar (golden)
def test_golden_de_varios_pedidos() -> None:
    indices = _indices()
    casos = _casos()
    assert len(casos) >= 15 and any(len(c["esperado"]) >= 2 for c in casos) and any(len(c["esperado"]) <= 1 for c in casos)
    predichos = aciertos_p = exactos = 0
    for caso in casos:
        got = intenciones_del_texto(
            caso["texto"], indices[caso["catalogo"]], strong=0.80, margin=0.15, min_score=0.45, max_candidates=6
        )
        predichos += len(got)
        aciertos_p += len(set(got) & set(caso["esperado"]))
        exactos += int(list(got) == caso["esperado"])
    # Precisión: nunca abre algo que no se pidió (la pila en `active` crearía tareas de más).
    assert aciertos_p == predichos, (aciertos_p, predichos)
    assert exactos / len(casos) >= 0.85, exactos


def test_no_repite_ni_abre_lo_que_ya_esta_abierto() -> None:
    ix = _indices()["planes"]
    texto = "quiero el plan premium y una demo el jueves"
    assert intenciones_del_texto(
        texto, ix, strong=0.80, margin=0.15, min_score=0.45, max_candidates=6, abiertas=(PLAN,)
    ) == (CITA,)
    assert intenciones_del_texto("", ix, strong=0.80, margin=0.15, min_score=0.45, max_candidates=6) == ()


# ---------------------------------------------------------------- plan
def test_plan_sin_foco() -> None:
    assert plan_de_pila([PLAN, CITA], con_foco=False, abiertas=0, max_stack=2) == {
        "abre": [PLAN], "pausa": [CITA], "sobrantes": []
    }
    assert plan_de_pila([PLAN, CITA, "x"], con_foco=False, abiertas=0, max_stack=2) == {
        "abre": [PLAN], "pausa": [CITA], "sobrantes": ["x"]
    }
    # Una sola intención sin foco: decide el router como hoy.
    assert plan_de_pila([PLAN], con_foco=False, abiertas=0, max_stack=2) is None
    # `maxStack = 1` apaga la pila.
    assert plan_de_pila([PLAN, CITA], con_foco=False, abiertas=0, max_stack=1) is None


def test_plan_con_foco_es_digresion() -> None:
    assert plan_de_pila([CITA], con_foco=True, abiertas=1, max_stack=2) == {"abre": [], "pausa": [CITA], "sobrantes": []}
    # Pila llena: todo sobra.
    assert plan_de_pila([CITA], con_foco=True, abiertas=2, max_stack=2) == {"abre": [], "pausa": [], "sobrantes": [CITA]}


def test_plan_sin_foco_con_pausadas() -> None:
    # Una pausada de antes ocupa un lugar: abre una y la segunda sobra.
    assert plan_de_pila([PLAN, CITA], con_foco=False, abiertas=1, max_stack=2) == {
        "abre": [PLAN], "pausa": [], "sobrantes": [CITA]
    }


# ---------------------------------------------------------------- banderas y sombra
def test_banderas_desconocidas_son_lo_mas_apagado() -> None:
    assert Colaboracion.desde({"pila": "turbo", "verificar_horarios": "x"}) == Colaboracion()
    assert Colaboracion.desde(None) == Colaboracion(pila="off", verificar_horarios="shadow", verificar_preguntas="shadow")
    assert Colaboracion.desde({"pila": "shadow", "verificar_preguntas": "active"}).verificar_preguntas == "active"


def test_la_pila_se_suma_a_la_sombra_sin_pisar_la_ruta() -> None:
    from core.kernel import decisions

    plan = {"abre": [PLAN], "pausa": [CITA], "sobrantes": []}
    agente = SimpleNamespace(key="ventas", instance_id="i-1")
    ruta = SimpleNamespace(
        candidates=(), source="trigger", rule_id=None, confidence=0.9, pila_sombra=plan,
        sombra=lambda: {"route_source": "trigger", "agent_key": agente.key},
    )
    turno = SimpleNamespace(decidido=False, candidates=None, route_source=None, rule_id=None, confidence=None, shadow_route=None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(decisions, "turno_actual", lambda: turno)
        decisions.anotar_ruta(ruta, activa=True)
        assert turno.shadow_route == {"pila": plan} and turno.route_source == "trigger"
        decisions.anotar_ruta(ruta, activa=False)
        assert turno.shadow_route == {"route_source": "trigger", "agent_key": "ventas", "pila": plan}


def test_metricas_de_pila() -> None:
    r = metricas_de_pila([([PLAN, CITA], [PLAN, CITA]), ([PLAN, CITA], [PLAN]), ([CITA], [])])
    assert (r.turnos, r.predichos, r.reales, r.aciertos, r.completos) == (3, 5, 3, 3, 1)
    assert r.precision == pytest.approx(0.6) and r.recall == pytest.approx(1.0)


# ---------------------------------------------------------------- parte 2: tarjeta, handoff y tope
def test_tarjeta_con_pila_guia_faltas_en_pausa_y_sobrantes() -> None:
    from test_kernel_tarjeta import PROC, _slot, _tarea

    from core.kernel.tarjeta import renderizar_tarjeta, rotulos

    procs = {("pack_commerce.contratar_plan", "1.0.0"): PROC}
    foco = _tarea(slots={"plan": _slot("Premium")})
    pausa = _tarea(status="suspended", stack_position=1, procedure_id="pack_commerce.contratar_plan", stage="elegir")
    tarjeta = renderizar_tarjeta(
        [foco, pausa], procedimientos=lambda i, v: procs.get((i, v)), sobrantes=["Tomar un pedido"]
    )
    r = rotulos()
    assert r["guia_pila"] in tarjeta
    # En pausa: lo que falta se dice, pero no se pide.
    assert "  plan: falta (se pide cuando siga esta tarea)" in tarjeta
    assert r["sobrantes"].format(procedimientos='"Tomar un pedido"') in tarjeta
    # Sin pausadas no hay guía (el prompt de siempre no cambia).
    assert r["guia_pila"] not in renderizar_tarjeta([foco], procedimientos=lambda i, v: procs.get((i, v)))


def test_tarjeta_con_el_foco_de_otro_agente_no_da_instrucciones() -> None:
    from test_kernel_tarjeta import PROC, _tarea

    from core.kernel.tarjeta import renderizar_tarjeta, rotulos

    procs = {("pack_commerce.contratar_plan", "1.0.0"): PROC}
    tarjeta = renderizar_tarjeta([_tarea()], procedimientos=lambda i, v: procs.get((i, v)), foco_de_otro="Agenda")
    assert rotulos()["retomada_de_otro"].format(agente='"Agenda"') in tarjeta
    assert "FALTA · pedirlo" not in tarjeta and "Acciones de esta etapa" not in tarjeta


def test_el_handoff_no_se_lleva_las_pausadas_de_otro_agente() -> None:
    import uuid

    from core.kernel.handoff import propias

    yo, otro = uuid.uuid4(), uuid.uuid4()
    filas = [
        SimpleNamespace(id=1, status="active", stack_position=0, owner_instance_id=yo),
        SimpleNamespace(id=2, status="suspended", stack_position=1, owner_instance_id=otro),
        SimpleNamespace(id=3, status="suspended", stack_position=2, owner_instance_id=None),
        SimpleNamespace(id=4, status="suspended", stack_position=3, owner_instance_id=yo),
    ]
    assert [f.id for f in propias(filas, yo)] == [1, 3, 4]


def test_max_stack_de_la_release_del_turno(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.shared import release_vigente

    def con(contenido: Any) -> None:
        kernel = SimpleNamespace(content=contenido)
        monkeypatch.setattr(release_vigente, "kernel_del_turno", lambda: kernel)

    con({"procedures": {"maxStack": 3}})
    assert release_vigente.max_stack() == 3
    con({"procedures": {"maxStack": 9}})
    assert release_vigente.max_stack() == 5
    con({})
    assert release_vigente.max_stack() == 2
    monkeypatch.setattr(release_vigente, "kernel_del_turno", lambda: None)
    assert release_vigente.max_stack() == 2

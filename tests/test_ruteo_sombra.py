"""HU-AOS-17 · reporte del enrutamiento (puro): la verdad del tráfico real, el golden contra una
release, la leyenda que dice qué mide y por qué sale vacío, y que el golden del repo viaja en la imagen."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts.kernelspec import Routing

from core.kernel.disparadores import Exponente, Indice
from core.kernel.ruteo_sombra import (
    GOLDEN_POR_DEFECTO,
    CasoGolden,
    Reporte,
    cargar_golden,
    cumple_meta,
    evaluar_golden,
    leyenda_trafico,
    metricas,
)
from core.shared.release_vigente import AgenteCongelado

PLAN = "pack_subscriptions.contratar_plan"
CITA = "pack_calendar.agendar_cita"
PEDIDO = "pack_commerce.tomar_pedido"


def _agente(key: str, procs: list[str], *, defecto: bool = False) -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=key, role="specialist", version_id=uuid.uuid4(),
        version=1, contract="agentspec/3", content={}, is_default=defecto,
        procedimientos=tuple((p, "1.0.0") for p in procs),
    )


class _Release:
    """Lo mínimo de una `ReleaseCongelada` que usa el router y el golden."""

    def __init__(self, ejemplos: dict[str, tuple[str, ...]], routing: dict[str, Any] | None = None) -> None:
        self.ejemplos = ejemplos
        self.componentes = (_agente("ventas", [p for p in ejemplos if p != CITA], defecto=True), _agente("agenda", [CITA]))
        self.con_procedimientos_por_agente = True
        self._routing = Routing.model_validate({"defaultAgent": "ventas", **(routing or {})})
        self._indice = Indice([Exponente(p, e) for p, e in ejemplos.items()])

    def ruteo(self) -> Routing:
        return self._routing

    def procedimientos(self) -> dict[str, Any]:
        return dict.fromkeys(self.ejemplos)

    def foco(self, _dueno: Any = None) -> AgenteCongelado:
        return self.componentes[0]

    def indice_de_disparadores(self) -> tuple[Indice, dict[str, str]]:
        return self._indice, {p: "agenda" if p == CITA else "ventas" for p in self.ejemplos}


CASOS = [
    CasoGolden("quiero agendar una demo", CITA, "c"),
    CasoGolden("quiero el plan premium", PLAN, "c"),
    CasoGolden("hola, buenas tardes", None, "c"),
    CasoGolden("necesito una factura", None, "otro"),
    CasoGolden("quiero hacer un pedido", PEDIDO, "otro"),
]
EJEMPLOS = {CITA: ("quiero agendar una demo",), PLAN: ("quiero el plan premium", "lo quiero contratar")}


def test_el_golden_del_repo_viaja_con_la_imagen_y_se_lee() -> None:
    assert GOLDEN_POR_DEFECTO.parts[-3:] == ("evals", "ruteo", "golden.jsonl")
    casos = cargar_golden()
    assert len(casos) >= 60
    assert any(c.esperado is None for c in casos) and all(c.catalogo for c in casos)


def test_un_golden_mal_formado_dice_el_archivo_y_la_linea(tmp_path: Path) -> None:
    archivo = tmp_path / "g.jsonl"
    archivo.write_text('{"texto": "hola", "esperado": null}\n{"esperado": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"g\.jsonl:2"):
        cargar_golden(archivo)


def test_el_golden_mide_sin_tarea_ni_inicios_y_separa_lo_no_aplicable() -> None:
    g = evaluar_golden(_Release(EJEMPLOS), CASOS, "web")
    # Solo `tomar_pedido`, que la release no tiene, no aplica; los negativos sí (el saludo y la factura).
    assert (g.reporte.turnos, g.no_aplicables, g.sin_dictamen) == (4, 1, 0)
    por = {m.procedure_id: m for m in g.reporte.metricas}
    assert (por[CITA].precision, por[CITA].recall) == (1.0, 1.0)
    assert (por[PLAN].precision, por[PLAN].recall) == (1.0, 1.0)
    assert g.fallos == [] and cumple_meta(g.reporte) is True


def test_en_el_golden_ninguno_es_una_etiqueta_no_una_ausencia_de_verdad() -> None:
    pares = metricas([(CITA, None), (CITA, CITA)], verdad_completa=True)
    assert pares.metricas[0].sin_verdad == 0 and pares.metricas[0].precision == 0.5
    # Un golden sin catálogo es del negocio: se pronuncia sobre todo, y enrutar es un falso positivo.
    g = evaluar_golden(_Release(EJEMPLOS), [CasoGolden("quiero agendar una demo", None), CasoGolden("quiero el plan premium", PLAN)])
    assert [f[1:] for f in g.fallos] == [(None, CITA)] and g.sin_dictamen == 0


def test_un_negativo_con_un_procedimiento_ajeno_al_catalogo_queda_sin_verdad() -> None:
    # El catálogo «c» solo trae PLAN: que el router elija CITA (propio del negocio) no es un falso positivo.
    casos = [CasoGolden("quiero el plan premium", PLAN, "c"), CasoGolden("quiero agendar una demo para la factura", None, "c")]
    g = evaluar_golden(_Release(EJEMPLOS), casos)
    assert (g.reporte.turnos, g.sin_dictamen, g.fallos) == (2, 1, [])
    por = {m.procedure_id: m for m in g.reporte.metricas}
    assert (por[CITA].predichos, por[CITA].sin_verdad, por[CITA].precision) == (1, 1, None)


def test_un_negativo_dentro_del_catalogo_si_es_un_falso_positivo() -> None:
    casos = [
        CasoGolden("quiero agendar una demo", CITA, "c"),
        CasoGolden("quiero el plan premium", PLAN, "c"),
        CasoGolden("quiero agendar una demo para la factura", None, "c"),  # el router sí enruta a CITA
    ]
    g = evaluar_golden(_Release(EJEMPLOS), casos)
    assert (g.reporte.turnos, g.no_aplicables, g.sin_dictamen) == (3, 0, 0)
    assert [f[1:] for f in g.fallos] == [(None, CITA)]
    por = {m.procedure_id: m for m in g.reporte.metricas}
    assert por[CITA].precision == pytest.approx(0.5) and cumple_meta(g.reporte) is False


def test_una_regla_se_evalua_como_en_el_turno() -> None:
    release = _Release(EJEMPLOS, {"rules": [{"id": "r", "when": {"keywordAny": ["quiero el plan premium"]}, "start": PLAN}]})
    g = evaluar_golden(release, [CasoGolden("quiero el plan premium", PLAN, "c"), CasoGolden("quiero agendar una demo", CITA, "c")])
    assert g.fallos == [] and g.no_evaluadas == ()


def test_cumple_meta_ignora_lo_no_medible() -> None:
    assert cumple_meta(Reporte()) is None
    assert cumple_meta(metricas([(CITA, None)])) is None  # sin verdad: ni precisión ni recall


def test_la_leyenda_explica_el_cero_en_sombra() -> None:
    r = metricas([(CITA, None)] * 7)  # 7 predicciones, ninguna conversación inició nada
    por = r.metricas[0]
    assert (por.predichos, por.reales, por.sin_verdad, por.precision) == (7, 0, 7, None)
    notas = " ".join(leyenda_trafico(r))
    assert "sin verdad" in notas.lower() and "golden" in notas and "no es una precisión de 0" in notas


def test_la_leyenda_en_activo_dice_por_que_no_hay_sombra_y_que_las_reglas_no_se_miden() -> None:
    r = Reporte(reglas_activas=3)
    notas = " ".join(leyenda_trafico(r))
    assert "no escribe shadow_route" in notas and "3 en la ventana" in notas and "Sin turnos medibles" in notas


def test_el_cli_rechaza_una_fuente_o_un_golden_invalidos(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from cli.commands.db import app

    runner = CliRunner()
    malo = runner.invoke(app, ["reporte-ruteo", "--tenant", "x", "--fuente", "otra"])
    assert malo.exit_code == 1 and "--fuente debe ser" in malo.output
    falta = runner.invoke(app, ["reporte-ruteo", "--tenant", "x", "--golden", str(tmp_path / "no-existe.jsonl")])
    assert falta.exit_code == 1 and "Error" in falta.output

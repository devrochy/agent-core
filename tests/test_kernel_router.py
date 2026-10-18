"""HU-AOS-17 · decisión del router del Coordinador (pura): tarea en foco → reglas → disparadores →
la de siempre, estabilidad del dueño, modo efectivo, revisión del build y métricas de la sombra."""

from __future__ import annotations

import uuid
from typing import Any

from agentcore_contracts.kernelspec import Routing

from core.kernel import router
from core.kernel.disparadores import Exponente, Indice
from core.kernel.router import Entrada, TareaEnFoco, decidir, estabilizar, modo_efectivo
from core.kernel.ruteo_sombra import metricas
from core.releases import manifiesto as man
from core.releases.build import _Instancia, revisar_ruteo
from core.shared.release_vigente import AgenteCongelado

PLAN = "pack_commerce.contratar_plan"
CITA = "pack_calendar.agendar_cita"


def _agente(key: str, procs: list[str], role: str = "specialist", *, defecto: bool = False) -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=key, role=role, version_id=uuid.uuid4(),
        version=1, contract="agentspec/3", content={}, is_default=defecto,
        procedimientos=tuple((p, "1.0.0") for p in procs),
    )


class _Release:
    def __init__(self, *agentes: AgenteCongelado, por_agente: bool = True) -> None:
        self.componentes = agentes
        self.con_procedimientos_por_agente = por_agente
        self._indice = Indice([
            Exponente(PLAN, ("quiero el plan premium", "lo quiero contratar")),
            Exponente(CITA, ("quiero agendar una cita", "quiero agendar una demo")),
        ])

    def indice_de_disparadores(self) -> tuple[Indice, dict[str, str]]:
        return self._indice, {PLAN: "ventas", CITA: "agenda"}


VENTAS = _agente("ventas", [PLAN], defecto=True)
AGENDA = _agente("agenda", [CITA])
RELEASE: Any = _Release(VENTAS, AGENDA)


def _routing(**kw: Any) -> Routing:
    return Routing.model_validate({"defaultAgent": "ventas", **kw})


def test_disparador_fuerte_elige_al_especialista_que_declara_el_procedimiento() -> None:
    ruta = decidir(RELEASE, _routing(), Entrada(texto="quiero agendar una demo"), VENTAS)
    assert (ruta.agente, ruta.source, ruta.procedure_id) == (AGENDA, "trigger", CITA)
    assert ruta.confidence is not None and ruta.confidence >= 0.8
    assert ruta.candidates[0] == {"procedure_id": CITA, "owner": "agenda", "score": ruta.confidence}
    assert ruta.start is None  # el disparador no inicia: elige el hilo


def test_sin_candidato_fuerte_atiende_el_de_siempre() -> None:
    ruta = decidir(RELEASE, _routing(), Entrada(texto="hola, ¿quiénes son?"), VENTAS)
    assert (ruta.agente, ruta.source, ruta.candidates) == (VENTAS, "default", ())


def test_la_tarea_en_foco_manda_sin_clasificar() -> None:
    tarea = TareaEnFoco(uuid.uuid4(), PLAN, "datos", VENTAS.instance_id)
    ruta = decidir(RELEASE, _routing(), Entrada(texto="quiero agendar una demo", tarea=tarea), AGENDA)
    assert (ruta.agente, ruta.source, ruta.focus_task_id, ruta.candidates) == (VENTAS, "task", tarea.task_id, ())


def test_tarea_sin_dueno_la_atiende_quien_declara_su_procedimiento() -> None:
    tarea = TareaEnFoco(uuid.uuid4(), CITA, "elegir", None)
    assert decidir(RELEASE, _routing(), Entrada(tarea=tarea), VENTAS).agente == AGENDA


def test_regla_de_boton_inicia_el_procedimiento_con_su_dueno() -> None:
    routing = _routing(rules=[{"id": "boton_plan", "when": {"interactivePayloadPrefix": "plan:"}, "start": PLAN}])
    ruta = decidir(RELEASE, routing, Entrada(texto="Pagar", payload="plan:premium"), AGENDA)
    assert (ruta.agente, ruta.source, ruta.rule_id, ruta.start, ruta.confidence) == (VENTAS, "rule", "boton_plan", PLAN, 1.0)
    # Sin el payload la regla no aplica.
    assert decidir(RELEASE, routing, Entrada(texto="Pagar"), AGENDA).source == "default"


def test_regla_con_tarea_en_foco_solo_si_es_sobre_su_etapa() -> None:
    tarea = TareaEnFoco(uuid.uuid4(), PLAN, "pagar", VENTAS.instance_id)
    otra = _routing(rules=[{"id": "palabra", "when": {"keywordAny": ["agenda"]}, "agent": "agenda"}])
    assert decidir(RELEASE, otra, Entrada(texto="agenda", tarea=tarea), VENTAS).source == "task"
    etapa = _routing(rules=[{"id": "en_pago", "when": {"flowStage": {"procedure": PLAN, "stage": "pagar"}}, "agent": "agenda"}])
    ruta = decidir(RELEASE, etapa, Entrada(texto="x", tarea=tarea), VENTAS)
    assert (ruta.agente, ruta.source, ruta.start) == (AGENDA, "rule", None)


def test_condiciones_de_las_reglas() -> None:
    regla = _routing(rules=[{
        "id": "mora", "agent": "agenda",
        "when": {"customerFact": {"namespace": "cobranza", "key": "en_mora", "equals": True}, "channel": "whatsapp",
                 "withinHours": {"from": "22:00", "to": "06:00"}},
    }]).rules[0]
    base = {"channel": "whatsapp", "hechos": {("cobranza", "en_mora"): True}, "hora_local": "23:30"}
    assert router.cumple(regla, Entrada(**base))
    assert not router.cumple(regla, Entrada(**{**base, "hora_local": "12:00"}))
    assert not router.cumple(regla, Entrada(**{**base, "channel": "web"}))
    assert not router.cumple(regla, Entrada(**{**base, "hechos": {("cobranza", "en_mora"): "true"}}))
    assert not router.cumple(regla, Entrada(**{**base, "hechos": {}}))
    palabra = _routing(rules=[{"id": "k", "when": {"keywordAny": ["hablar con ventas"]}, "agent": "ventas"}]).rules[0]
    assert router.cumple(palabra, Entrada(texto="Quiero HABLAR con Ventas, ¿sí?"))
    assert not router.cumple(palabra, Entrada(texto="hablar con ventasx"))


def test_regla_a_un_agente_que_no_esta_se_ignora() -> None:
    routing = _routing(rules=[{"id": "x", "when": {"channel": "web"}, "agent": "cobranza"}])
    assert decidir(RELEASE, routing, Entrada(channel="web"), VENTAS).source == "default"


def test_release_1_sin_procedimientos_por_agente_atiende_el_de_siempre() -> None:
    vieja: Any = _Release(_agente("general", []), por_agente=False)
    general = vieja.componentes[0]
    routing = _routing(rules=[{"id": "b", "when": {"interactivePayloadPrefix": "plan:"}, "start": PLAN}])
    ruta = decidir(vieja, routing, Entrada(payload="plan:x"), general)
    assert (ruta.agente, ruta.source, ruta.start) == (general, "rule", PLAN)


def test_estabilidad_frena_un_cambio_de_dueno_de_mas() -> None:
    routing = _routing()
    entrada = Entrada(texto="quiero agendar una demo", dueno=VENTAS.instance_id)
    ruta = decidir(RELEASE, routing, entrada, VENTAS)
    assert estabilizar(ruta, entrada, VENTAS, routing, 1).agente == AGENDA
    frenada = estabilizar(ruta, entrada, VENTAS, routing, 2)
    assert (frenada.agente, frenada.source, frenada.frenada_por_estabilidad) == (VENTAS, "default", True)
    assert frenada.candidates == ruta.candidates


def test_modo_efectivo_y_sombra() -> None:
    assert modo_efectivo("active", _routing()) == "active"
    assert modo_efectivo("active", _routing(shadowMode=True)) == "shadow"
    assert modo_efectivo("shadow", _routing()) == "shadow"
    assert modo_efectivo("raro", None) == "off"
    sombra = decidir(RELEASE, _routing(), Entrada(texto="quiero agendar una demo"), VENTAS).sombra()
    assert sombra["agent_key"] == "agenda" and sombra["route_source"] == "trigger" and sombra["procedure_id"] == CITA
    assert "texto" not in sombra


def test_necesita_solo_lee_lo_que_las_reglas_usan() -> None:
    assert router.necesita(_routing()) == (set(), False)
    routing = _routing(rules=[{"id": "h", "when": {"customerFact": {"namespace": "n", "key": "k", "equals": 1}}, "agent": "x1"}])
    assert router.necesita(routing) == ({("n", "k")}, False)


def _componente(key: str, role: str, procs: tuple[tuple[str, str], ...] = ()) -> man.Componente:
    return man.Componente(
        instance_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()), agent_key=key, role=role,
        agent_version_id=str(uuid.uuid4()), version=1, content_hash="h", contract="x", procedures=procs,
    )


def test_el_build_rechaza_reglas_que_no_apuntan_a_la_release() -> None:
    core = _componente("coordinador", "core")
    ventas = _componente("ventas", "specialist", ((PLAN, "1.0.0"),))
    reglas = [
        {"id": "ok", "when": {"channel": "web"}, "agent": "ventas"},
        {"id": "ok2", "when": {"channel": "web"}, "start": PLAN},
        {"id": "mal", "when": {"channel": "web"}, "agent": "cobranza"},
        {"id": "mal2", "when": {"channel": "web"}, "start": CITA},
    ]
    instancias = [_Instancia(core, {"routing": {"rules": reglas}}), _Instancia(ventas, {})]
    problemas = revisar_ruteo(instancias, [core, ventas])
    assert len(problemas) == 2 and problemas[0].startswith("mal:") and problemas[1].startswith("mal2:")
    assert revisar_ruteo([_Instancia(core, {})], [core]) == []


def test_metricas_de_la_sombra() -> None:
    reporte = metricas([(CITA, CITA), (CITA, None), (None, PLAN), (PLAN, PLAN), (None, None)])
    por = {m.procedure_id: m for m in reporte.metricas}
    assert reporte.turnos == 5
    assert (por[CITA].precision, por[CITA].recall) == (0.5, 1.0)
    assert (por[PLAN].precision, por[PLAN].recall) == (1.0, 0.5)
    assert metricas([]).metricas == []


def test_payload_en_redis_con_el_negocio_primero(monkeypatch: Any) -> None:
    import asyncio

    guardado: dict[str, Any] = {}

    class _Redis:
        async def set(self, clave: str, valor: str, ex: int) -> None:
            guardado[clave] = (valor, ex)

        async def getdel(self, clave: str) -> Any:
            return (guardado.pop(clave, (None,))[0] or "").encode() or None

    monkeypatch.setattr("core.memory.working.get_redis", lambda: _Redis())
    t, c = uuid.uuid4(), uuid.uuid4()
    asyncio.run(router.guardar_payload(t, c, "plan:" + "x" * 1000))
    ((clave, (valor, ex)),) = guardado.items()
    assert clave == f"aos:ruteo:payload:{t}:{c}" and len(valor) == router.MAX_PAYLOAD and ex <= 24 * 3600
    assert asyncio.run(router.tomar_payload(t, c)).startswith("plan:")
    assert asyncio.run(router.tomar_payload(t, c)) == ""  # se consume una vez

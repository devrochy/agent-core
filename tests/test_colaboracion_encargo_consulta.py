"""Encargo y consulta entre agentes, parte pura (HU-AOS-19 parte 2, adenda de ADR-0116 §4-§5).

Qué es encargable, qué consulta se ofrece, el subconjunto de JSON Schema del kernel, el linter del
contrato 3.2.0 y los schemas de los comandos.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from agentcore_contracts import AgentSpecV3, Procedure, __version__

from core.agents.contrato_v3 import lint_agentspec
from core.kernel import colaboracion
from core.kernel.esquema_salida import errores, no_soportadas
from core.kernel.procedures import comandos
from core.kernel.procedures.catalogo import ActionInfo

DEMO: dict[str, Any] = {
    "contract": "procedure/1",
    "id": "pack_calendar.reservar_demo",
    "version": "1.0.0",
    "title": "Reservar una demo",
    "triggers": {"description": "Reservar una demo", "examples": ["quiero una demo"]},
    "slots": {
        "fecha": {"type": "date"},
        "hora": {"type": "time"},
        "nombre": {"type": "string", "dataClass": "pii"},
    },
    "stages": [
        {"id": "datos", "goal": "Tomar los datos", "exitWhen": {"slots": ["fecha", "hora", "nombre"]}},
        {"id": "listo", "goal": "Listo"},
    ],
    "recovery": {"actionError": "explicar_y_reintentar_una_vez", "timeout": "relevo_humano"},
}


def _demo(**cambios: Any) -> Procedure:
    datos = copy.deepcopy(DEMO)
    for ruta, valor in cambios.items():
        destino = datos
        partes = ruta.split("__")
        for p in partes[:-1]:
            destino = destino[int(p)] if p.isdigit() else destino[p]
        destino[partes[-1]] = valor
    return Procedure.model_validate(datos)


@dataclass
class _Agente:
    key: str
    content: dict[str, Any]
    instance_id: uuid.UUID = field(default_factory=uuid.uuid4)


# ------------------------------------------------------------------ encargable
def test_un_procedimiento_de_datos_es_encargable() -> None:
    assert colaboracion.encargable(_demo())


@pytest.mark.parametrize(
    "cambio",
    [
        {"stages__0__exitWhen": {"action": "reservar", "ok": True}},
        {"stages__0__exitWhen": {"slots": ["fecha"], "confirmed": ["fecha"]}},
        {"stages__0__preconditions": ["check_availability"]},
        {"slots__fecha": {"type": "date", "confirm": True}},
        {"slots__nombre": {"type": "string", "dataClass": "payment"}},
        {"slots__fecha": {"type": "date", "setBy": "action:reservar"}},
        {"slots__fecha": {"type": "location"}},
    ],
)
def test_lo_que_no_se_completa_con_datos_no_es_encargable(cambio: dict[str, Any]) -> None:
    assert not colaboracion.encargable(_demo(**cambio))


def test_los_procedimientos_de_los_packs_hoy_no_son_encargables() -> None:
    import json
    from pathlib import Path

    for ruta in Path("packages").glob("pack-*/procedures/*.json"):
        assert not colaboracion.encargable(Procedure.model_validate(json.loads(ruta.read_text(encoding="utf-8")))), ruta


def test_encargables_del_destino_son_los_expuestos_y_publicados() -> None:
    demo = _demo()
    destino = _Agente("agenda", {"exposes": {"procedures": [demo.id, "pack_x.no_publicado"]}})
    assert colaboracion.encargables_de(destino, {demo.id: demo}) == {demo.id: demo}
    assert colaboracion.encargables_de(_Agente("agenda", {}), {demo.id: demo}) == {}
    assert colaboracion.clases_de(demo, ["fecha", "nombre"]) == ("pii", "public")


# ------------------------------------------------------------------ consultable
def _destino_con_consulta(accion: str = "estado_demo", **binding: Any) -> _Agente:
    return _Agente(
        "suscripciones",
        {
            "tools": [{"tool": accion, "enabled": True, **binding}],
            "exposes": {"consultas": [{"id": "subs.estado", "description": "Estado", "action": accion, "outputSchema": {"type": "object"}}]},
        },
    )


def test_consulta_con_accion_propia_y_fuera_de_etapa() -> None:
    plugin = {"estado_demo": "pack_subscriptions"}.get
    salida = colaboracion.consultas_de(
        _destino_con_consulta(), prohibidas=frozenset(), packs=frozenset({"pack_subscriptions"}), plugin_de=plugin,
        es_lectura=lambda _a: True,
    )
    assert list(salida) == ["subs.estado"] and salida["subs.estado"].accion == "estado_demo"
    # Sin la marca de solo lectura no se ofrece (fail-closed).
    assert colaboracion.consultas_de(
        _destino_con_consulta(), prohibidas=frozenset(), packs=frozenset({"pack_subscriptions"}), plugin_de=plugin
    ) == {}


@pytest.mark.parametrize(
    ("destino", "prohibidas", "packs"),
    [
        (_destino_con_consulta(), frozenset({"estado_demo"}), frozenset({"pack_subscriptions"})),  # la gobierna el motor
        (_destino_con_consulta(requiresConfirmation=True), frozenset(), frozenset({"pack_subscriptions"})),
        (_destino_con_consulta(), frozenset(), frozenset()),  # fuera del scope de la release
    ],
)
def test_consulta_que_no_se_ofrece(destino: _Agente, prohibidas: frozenset[str], packs: frozenset[str]) -> None:
    plugin = {"estado_demo": "pack_subscriptions"}.get
    assert colaboracion.consultas_de(
        destino, prohibidas=prohibidas, packs=packs, plugin_de=plugin, es_lectura=lambda _a: True
    ) == {}


def test_sin_accion_o_con_accion_ajena_no_se_ofrece() -> None:
    sin = _Agente("s", {"exposes": {"consultas": [{"id": "s.x", "outputSchema": {}}]}})
    assert colaboracion.consultas_de(sin, prohibidas=frozenset(), packs=frozenset(), plugin_de=lambda _a: "core") == {}
    ajena = _Agente("s", {"tools": [], "exposes": {"consultas": [{"id": "s.x", "action": "otra"}]}})
    assert colaboracion.consultas_de(ajena, prohibidas=frozenset(), packs=frozenset(), plugin_de=lambda _a: "core") == {}


# ------------------------------------------------------------------ esquema de salida
def test_subconjunto_de_json_schema() -> None:
    esquema = {
        "type": "object",
        "properties": {"estado": {"type": "string", "enum": ["activa", "vencida"]}, "dias": {"type": "integer"}},
        "required": ["estado"],
        "additionalProperties": False,
    }
    assert no_soportadas(esquema) == []
    assert errores({"estado": "activa", "dias": 3}, esquema) == []
    assert errores({"estado": "otra"}, esquema) == ["$.estado: enum"]
    assert errores({"dias": "3"}, esquema) == ["$.estado: requerido", "$.dias: tipo"]
    assert errores({"estado": "activa", "extra": 1}, esquema) == ["$.extra: no permitido"]
    assert no_soportadas({"type": "object", "pattern": "x", "properties": {"a": {"oneOf": []}}}) == [
        "$.pattern",
        "$.properties.a.oneOf",
    ]
    assert errores({}, {"type": "object", "pattern": "x"}) == ["$: schema no soportado"]


# ------------------------------------------------------------------ contrato 3.2.0
CATALOGO: dict[str, Any] = {
    "estado_demo": ActionInfo("estado_demo", "pack_subscriptions", solo_lectura=True),
    "otra": ActionInfo("otra", "pack_subscriptions"),
    "escribe": ActionInfo("escribe", "pack_subscriptions"),
}

def test_contrato_3_2_0_y_linter_de_consultas() -> None:
    assert __version__ == "3.2.0"
    base = {"model": {"primary": "deepseek-flash"}, "tools": [{"tool": "estado_demo"}]}
    bien = AgentSpecV3.model_validate(
        {**base, "exposes": {"consultas": [{"id": "subs.estado", "action": "estado_demo", "outputSchema": {"type": "object"}}]}}
    )
    assert [i.code for i in lint_agentspec(bien, CATALOGO)] == []
    ajena = AgentSpecV3.model_validate({**base, "exposes": {"consultas": [{"id": "subs.estado", "action": "otra"}]}})
    assert "consulta_sin_accion_del_agente" in [i.code for i in lint_agentspec(ajena, CATALOGO)]
    rara = AgentSpecV3.model_validate(
        {**base, "exposes": {"consultas": [{"id": "subs.estado", "action": "estado_demo", "outputSchema": {"oneOf": []}}]}}
    )
    assert "schema_de_salida_no_soportado" in [i.code for i in lint_agentspec(rara, CATALOGO)]
    escribe = AgentSpecV3.model_validate(
        {"model": {"primary": "deepseek-flash"}, "tools": [{"tool": "escribe"}],
         "exposes": {"consultas": [{"id": "subs.estado", "action": "escribe"}]}}
    )
    assert "consulta_con_accion_que_escribe" in [i.code for i in lint_agentspec(escribe, CATALOGO)]
    # Sin `action` no se revisa el schema (nada se ejecuta).
    sin = AgentSpecV3.model_validate({**base, "exposes": {"consultas": [{"id": "subs.estado", "outputSchema": {"oneOf": []}}]}})
    assert [i.code for i in lint_agentspec(sin, CATALOGO)] == []


# ------------------------------------------------------------------ comandos
def test_encargar_solo_con_foco_y_consultar_sin_el() -> None:
    assert comandos.aplicables(foco=None, candidatos=[], encargos=True, consultas=True) == [comandos.CONSULTAR]


def test_schemas_de_encargar_y_consultar() -> None:
    demo = _demo()
    consulta = colaboracion.Consulta("subs.estado", 'Estado "real"', "estado_demo", {"type": "object"})
    encargar, consultar = comandos.herramientas(
        [comandos.ENCARGAR, comandos.CONSULTAR], foco=None, candidatos=[],
        oferta_encargos=[("agenda", demo)], oferta_consultas=[("suscripciones", consulta)],
    )
    props = encargar.parameters["properties"]
    assert props["agente"]["enum"] == ["agenda"] and props["procedimiento"]["enum"] == [demo.id]
    assert sorted(props["entradas"]["properties"]) == ["fecha", "hora", "nombre"]
    assert props["entradas"]["additionalProperties"] is False
    assert consultar.parameters["properties"]["consulta"]["enum"] == ["subs.estado"]
    assert '\\"real\\"' in consultar.description  # la descripción va escapada (dato)
    assert comandos.argumentos_invalidos(comandos.ENCARGAR, {"agente": "a", "procedimiento": "p", "entradas": {}}) is None
    assert comandos.argumentos_invalidos(comandos.ENCARGAR, {"agente": "a", "procedimiento": "p"}) is not None
    assert comandos.argumentos_invalidos(comandos.CONSULTAR, {"agente": "a", "consulta": "c"}) is None


# ------------------------------------------------------------------ ejecutor de consultas del loop
async def test_el_ejecutor_de_consultas_usa_la_identidad_del_destino(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import core.agents.spec_de_release as spec_mod
    from core.orchestrator import loop

    vistas: list[Any] = []

    async def _ejecutar(conversation: Any, customer: Any, llamada: Any, spec: Any = None, **kw: Any) -> dict[str, Any]:
        vistas.append((llamada.id, llamada.name, kw))
        return {"estado": "activa"}

    actual = SimpleNamespace(tenant_id=uuid.uuid4(), skills=(), tenant_plugins=())
    monkeypatch.setattr(loop.release_vigente, "vigente", lambda: actual)
    monkeypatch.setattr(loop.release_vigente, "kernel_del_turno", lambda: None)
    confirmar = {"valor": False}
    spec = SimpleNamespace(tools=[SimpleNamespace(tool="estado_demo", requiresConfirmation=False)])
    monkeypatch.setattr(spec_mod, "spec_v2_desde_release", lambda *a, **k: spec)
    monkeypatch.setattr(loop, "_execute_tool_safely", _ejecutar)
    destino = _Agente("suscripciones", {})
    ejecutar = loop._ejecutor_de_consulta(SimpleNamespace(), SimpleNamespace())
    assert await ejecutar(destino, "estado_demo", {}, "query-1") == {"estado": "activa"}
    ((clave, nombre, kw),) = vistas
    assert (clave, nombre) == ("query-1", "estado_demo") and kw == {"identidad": destino, "solo_lectura": True}
    spec.tools[0].requiresConfirmation = True
    assert await ejecutar(destino, "estado_demo", {}, "query-2") == {"error": "consulta_requiere_confirmacion"}
    assert len(vistas) == 1 and confirmar["valor"] is False

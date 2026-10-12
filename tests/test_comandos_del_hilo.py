"""HU-AOS-06 · comandos del hilo como tools del LLM: oferta, schemas y textos (unitarios).

Sin BD ni red. Lo que toca la base (el motor aplicando el comando, el resultado con la tarjeta y
el loop) está en `tests/integration/test_comandos_en_el_loop.py`.
"""

from __future__ import annotations

import json
import types
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts import Procedure

from core.kernel.procedures import comandos, motor
from core.kernel.procedures.ejecucion import ContextoMotor, _Foco
from core.kernel.state import Proyeccion
from core.kernel.tarjeta import RotulosInvalidos
from core.mcp.types import MCPTool

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "procedures"


def _proc(nombre: str = "contratar_plan") -> Procedure:
    return Procedure.model_validate(json.loads((FIXTURES / f"{nombre}.json").read_text(encoding="utf-8")))


def _tarea(stage: str = "datos", pendiente: dict[str, Any] | None = None) -> Proyeccion:
    return Proyeccion(
        procedure_id="pack_commerce.contratar_plan", procedure_version="1.0.0", status="active", stage=stage,
        stack_position=0, last_seq=1, created_at=T0, updated_at=T0, pending_confirmation=pendiente,
    )


async def _nada(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    return {}


def _herramienta(nombre: str, plugin_id: str = "pack_commerce") -> MCPTool:
    return MCPTool(name=nombre, description=nombre, parameters={"type": "object", "properties": {}},
                   handler=_nada, plugin_id=plugin_id)


def _motor(*, modo: str = "activo", foco: tuple[Proyeccion, Procedure] | None = None, abiertas=frozenset(),
           base: list[MCPTool] | None = None) -> ContextoMotor:
    proc = _proc()
    candidatas = [_herramienta("informar_plan"), _herramienta("generar_cotizacion"), _herramienta("crear_pedido")]
    _f = None
    if foco is not None:
        tarea, p = foco
        stage = motor.etapa(p, tarea.stage)
        assert stage is not None
        _f = _Foco(fila=types.SimpleNamespace(id=uuid.uuid4()), tarea=tarea, proc=p, stage=stage)  # type: ignore[arg-type]
    return ContextoMotor(
        modo=modo,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(), conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), channel="whatsapp",
        procedimientos={proc.id: proc}, herramientas_base=base or [_herramienta("escalate_to_human", "core")],
        candidatas=candidatas, foco=_f, abiertas=abiertas,
    )


# ---------------------------------------------------------------- textos


def test_el_playbook_de_comandos_trae_todos_los_textos() -> None:
    t = comandos.textos()
    assert set(t) == set(comandos.TEXTOS)
    assert "{comando}" not in comandos.texto("comando_no_disponible", comando="x")


def test_un_playbook_incompleto_falla_cerrado() -> None:
    with pytest.raises(RotulosInvalidos):
        comandos.parsear_textos("## iniciar_procedimiento\nhola\n")
    contenido = "\n".join(f"## {k}\nuno {{nada}}" for k in comandos.TEXTOS)
    with pytest.raises(RotulosInvalidos):
        comandos.parsear_textos(contenido)


# ---------------------------------------------------------------- oferta


def test_sin_foco_solo_se_ofrece_iniciar_con_candidatos() -> None:
    proc = _proc()
    assert comandos.aplicables(foco=None, candidatos=[proc]) == [comandos.INICIAR]
    assert comandos.aplicables(foco=None, candidatos=[]) == []


def test_con_foco_los_que_aplican_a_la_etapa() -> None:
    proc = _proc()
    assert comandos.aplicables(foco=(_tarea(), proc), candidatos=[proc]) == [comandos.ACTUALIZAR, comandos.CANCELAR]
    pendiente = _tarea(pendiente={"kind": "slot", "name": "correo", "seq": 3})
    assert comandos.aplicables(foco=(pendiente, proc), candidatos=[]) == [
        comandos.ACTUALIZAR, comandos.CONFIRMAR, comandos.CANCELAR,
    ]


def test_sin_datos_escribibles_no_se_ofrece_actualizar() -> None:
    datos = json.loads((FIXTURES / "contratar_plan.json").read_text(encoding="utf-8"))
    datos["slots"] = {k: v for k, v in datos["slots"].items() if "setBy" in v}
    datos["claims"] = []
    proc = Procedure.model_validate(datos)
    assert comandos.aplicables(foco=(_tarea(), proc), candidatos=[]) == [comandos.CANCELAR]


# ---------------------------------------------------------------- schemas


def test_iniciar_lleva_el_enum_de_candidatos_y_los_textos_escapados() -> None:
    proc = _proc()
    [tool] = comandos.herramientas([comandos.INICIAR], foco=None, candidatos=[proc])
    assert tool.parameters["properties"]["procedure_id"]["enum"] == ["pack_commerce.contratar_plan"]
    assert tool.parameters["required"] == ["procedure_id"] and tool.parameters["additionalProperties"] is False
    assert '"Contratar un plan"' in tool.description  # el título va como cadena JSON
    assert '"El cliente quiere comprar, contratar o pagar un plan"' in tool.description


def test_actualizar_solo_con_los_datos_que_escribe_el_modelo() -> None:
    proc = _proc()
    [tool] = comandos.herramientas([comandos.ACTUALIZAR], foco=(_tarea(), proc), candidatos=[])
    slots = tool.parameters["properties"]["slots"]
    assert slots["additionalProperties"] is False
    # `variante_id`, `cotizacion_id`, `pedido_id` y `link_pago` los fija el servidor o un botón.
    assert set(slots["properties"]) == {"periodicidad", "correo"}
    assert slots["properties"]["periodicidad"] == {"type": "string", "enum": ["mensual", "anual"]}
    assert slots["properties"]["correo"] == {"type": "string", "format": "email"}


def test_argumentos_fuera_del_schema() -> None:
    assert comandos.argumentos_invalidos(comandos.INICIAR, {"procedure_id": "x"}) is None
    assert comandos.argumentos_invalidos(comandos.INICIAR, {"procedure_id": "x", "task_id": "y"})
    assert comandos.argumentos_invalidos(comandos.ACTUALIZAR, {"slots": "correo"})
    assert comandos.argumentos_invalidos(comandos.CANCELAR, {"motivo": "porque sí"})
    assert comandos.argumentos_invalidos(comandos.CONFIRMAR, {}) is None


async def test_el_handler_de_un_comando_no_se_llama() -> None:
    [tool] = comandos.herramientas([comandos.CANCELAR], foco=(_tarea(), _proc()), candidatos=[])
    with pytest.raises(RuntimeError):
        await tool.handler({}, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- ContextoMotor


def test_activo_sin_foco_agrega_iniciar_y_quita_la_declarativa_homonima() -> None:
    base = [_herramienta("escalate_to_human", "core"), _herramienta("cancelar_tarea", "tenant")]
    m = _motor(base=base)
    nombres = [t.name for t in m.herramientas()]
    assert nombres == ["escalate_to_human", comandos.INICIAR]
    assert m.es_comando(comandos.CANCELAR)


def test_un_procedimiento_con_tarea_abierta_no_es_candidato() -> None:
    m = _motor(abiertas=frozenset({"pack_commerce.contratar_plan"}))
    assert [t.name for t in m.herramientas()] == ["escalate_to_human"]


def test_con_foco_las_acciones_de_la_etapa_mas_los_comandos() -> None:
    m = _motor(foco=(_tarea("confirmar"), _proc()))
    assert [t.name for t in m.herramientas()] == [
        "generar_cotizacion", "escalate_to_human", comandos.ACTUALIZAR, comandos.CANCELAR,
    ]
    # `confirmar_cotizacion` está en la etapa pero no se ofrece (no hay tool): no se anuncia.
    assert m._acciones() == ["generar_cotizacion"]


def test_en_sombra_no_hay_comandos() -> None:
    m = _motor(modo="sombra")
    assert [t.name for t in m.herramientas()] == ["escalate_to_human"]
    assert m.comandos() == [] and not m.es_comando(comandos.INICIAR)


async def test_un_comando_que_no_aplica_no_se_aplica(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    m = _motor()

    async def no_releer() -> None:
        return None

    async def no_debe(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no se aplica")

    monkeypatch.setattr(m, "_releer_seguro", no_releer)
    monkeypatch.setattr(m, "aplicar_comando", no_debe)
    r = await m.ejecutar_comando(comandos.CANCELAR, {})
    # Sin foco y con candidatos, la tarjeta lleva la pista de iniciar (adenda 2026-10-12).
    assert r == {
        "ok": False, "state_card": m.tarjeta, "allowed_actions": [],
        "errors": [comandos.texto("comando_no_disponible", comando=comandos.CANCELAR)],
    }
    r2 = await m.ejecutar_comando(comandos.INICIAR, {"procedure_id": "pack_commerce.contratar_plan", "task_id": "x"})
    assert not r2["ok"] and "no reconoce" in r2["errors"][0]


def test_ninguna_accion_del_binario_usa_el_nombre_de_un_comando() -> None:
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    assert not set(catalogo_de_acciones(plugin_manager.scan())) & comandos.NOMBRES

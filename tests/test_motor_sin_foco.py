"""Adenda 2026-10-12 de ADR-0113 · sin tarea en foco no hay camino de siempre (unitarios).

Hallazgo de la prueba real en test (2026-10-12): con `aos_procedures` encendida y sin tarea en
foco, el modelo cerró una venta con `confirmar_cotizacion` → `crear_pedido` sin iniciar el
procedimiento. Ahora, con el motor activo y sin foco:

- las puertas de confirmación de los procedimientos disponibles (`motor.acciones_de_puerta`) no se
  ofrecen y, si el modelo igual las llama, se niegan con la pista de qué recorrido iniciar;
- las consultas y lo que no se confirma con el cliente se siguen ofreciendo;
- la tarjeta lleva la pista de iniciar el recorrido (playbook del kernel, no del negocio);
- con una tarea activa que el motor no gobierna queda el flujo v2 de respaldo.

En sombra o con la bandera apagada nada cambia. Sin BD ni red: el lock y las lecturas se simulan.
Lo que toca la base y el loop real está en `tests/integration/test_motor_sin_foco_en_el_loop.py`.
"""

from __future__ import annotations

import json
import types
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts import Procedure

from core.kernel import tarjeta
from core.kernel.procedures import comandos, ejecucion, motor
from core.kernel.procedures.ejecucion import ContextoMotor, _Foco
from core.kernel.procedures.textos import mensajes
from core.kernel.state import Proyeccion
from core.mcp.types import MCPTool

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
PLAN = "pack_commerce.contratar_plan"
PEDIDO = "pack_commerce.tomar_pedido"


def _publicado(pack: str, nombre: str) -> Procedure:
    ruta = ROOT / "packages" / pack / "procedures" / f"{nombre}.json"
    return Procedure.model_validate(json.loads(ruta.read_text(encoding="utf-8")))


def _todos() -> list[Procedure]:
    return [
        Procedure.model_validate(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(ROOT.glob("packages/pack-*/procedures/*.json"))
    ]


async def _nada(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    return {}


def _tool(nombre: str, plugin_id: str = "pack_commerce") -> MCPTool:
    return MCPTool(name=nombre, description=nombre, parameters={"type": "object", "properties": {}},
                   handler=_nada, plugin_id=plugin_id)


BASE = [
    _tool("informar_plan"), _tool("consultar_inventario"), _tool("generar_cotizacion"),
    _tool("confirmar_cotizacion"), _tool("crear_pedido"), _tool("confirmar_pago_pedido"),
    _tool("escalate_to_human", "core"), _tool("sub_aplicar_cambio_plan", "core"),
]


def _motor(
    *, modo: str = "activo", foco_etapa: str | None = None, abiertas: frozenset[str] = frozenset(),
    sin_gobierno: str | None = None, tarjeta_base: str = "", procs: list[Procedure] | None = None,
) -> ContextoMotor:
    procs = procs if procs is not None else [_publicado("pack-commerce", "contratar_plan"),
                                             _publicado("pack-commerce", "tomar_pedido")]
    plan = next(p for p in procs if p.id == PLAN) if any(p.id == PLAN for p in procs) else None
    f = None
    if foco_etapa is not None and plan is not None:
        stage = motor.etapa(plan, foco_etapa)
        assert stage is not None
        tarea = Proyeccion(procedure_id=PLAN, procedure_version=plan.version, status="active", stage=foco_etapa,
                           stack_position=0, last_seq=1, created_at=T0, updated_at=T0)
        f = _Foco(fila=types.SimpleNamespace(id=uuid.uuid4()), tarea=tarea, proc=plan, stage=stage)  # type: ignore[arg-type]
    return ContextoMotor(
        modo=modo,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(), conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), channel="web",
        procedimientos={p.id: p for p in procs}, herramientas_base=list(BASE), candidatas=list(BASE),
        foco=f, abiertas=abiertas, sin_gobierno=sin_gobierno, tarjeta=tarjeta_base,
    )


def _nombres(m: ContextoMotor) -> list[str]:
    return [t.name for t in m.herramientas()]


# ---------------------------------------------------------------- la regla pura


def test_las_puertas_de_los_procedimientos_publicados() -> None:
    """Fija el conjunto derivado de los JSON de los packs: una puerta nueva se ve en CI."""
    por_proc = {p.id: motor.acciones_de_puerta(p) for p in _todos()}
    assert por_proc == {
        "pack_calendar.agendar_cita": {"create_calendar_event"},
        "pack_cobranza.promesa_de_pago": {"cob_registrar_promesa_saldo"},
        PLAN: {"confirmar_cotizacion"},
        PEDIDO: {"confirmar_cotizacion"},
        "pack_subscriptions.cambiar_plan": {"sub_aplicar_cambio_plan"},
        "pack_subscriptions.contratar_plan": {"sub_confirmar_inscripcion"},
    }


def test_las_consultas_no_son_puerta() -> None:
    todas = frozenset().union(*(motor.acciones_de_puerta(p) for p in _todos()))
    consultas = {
        "informar_plan", "consultar_inventario", "check_availability", "find_next_available",
        "list_appointment_types", "cob_consultar_saldo", "cob_opciones_promesa", "sub_consultar",
        # No se confirman con el cliente: crear_pedido exige una cotización aceptada y
        # confirmar_pago_pedido reenvía el link de un pedido que ya existe.
        "crear_pedido", "confirmar_pago_pedido", "verificar_pago_pedido", "generar_cotizacion",
    }
    assert not todas & consultas


# ---------------------------------------------------------------- oferta


def test_activo_sin_foco_oculta_las_puertas_y_ofrece_iniciar() -> None:
    nombres = _nombres(_motor())
    assert "confirmar_cotizacion" not in nombres and "sub_aplicar_cambio_plan" in nombres  # sin cambiar_plan
    assert {"informar_plan", "consultar_inventario", "generar_cotizacion", "crear_pedido",
            "confirmar_pago_pedido", "escalate_to_human", comandos.INICIAR} <= set(nombres)


def test_una_puerta_del_core_tambien_se_oculta_sin_foco() -> None:
    procs = [_publicado("pack-commerce", "contratar_plan"), _publicado("pack-subscriptions", "cambiar_plan")]
    nombres = _nombres(_motor(procs=procs))
    assert "sub_aplicar_cambio_plan" not in nombres and "confirmar_cotizacion" not in nombres


def test_con_foco_no_cambia() -> None:
    nombres = _nombres(_motor(foco_etapa="confirmar"))
    assert "confirmar_cotizacion" in nombres and comandos.INICIAR not in nombres


def test_una_tarea_sin_gobierno_deja_el_respaldo_v2_de_su_procedimiento() -> None:
    m = _motor(sin_gobierno=PLAN, abiertas=frozenset({PLAN}))
    assert "confirmar_cotizacion" in _nombres(m)
    assert m.tarjeta == ""


def test_el_respaldo_v2_no_libera_las_puertas_de_otro_procedimiento() -> None:
    procs = [_publicado("pack-commerce", "contratar_plan"), _publicado("pack-calendar", "agendar_cita")]
    m = _motor(procs=procs, sin_gobierno="pack_calendar.agendar_cita")
    assert "confirmar_cotizacion" not in _nombres(m)


def test_con_foco_una_puerta_del_core_de_otro_procedimiento_no_se_ofrece() -> None:
    procs = [_publicado("pack-commerce", "contratar_plan"), _publicado("pack-subscriptions", "cambiar_plan")]
    nombres = _nombres(_motor(procs=procs, foco_etapa="confirmar"))
    assert "sub_aplicar_cambio_plan" not in nombres and "escalate_to_human" in nombres


def test_con_todos_los_procedimientos_abiertos_las_puertas_siguen_ocultas() -> None:
    """Una tarea en pausa (HU-AOS-20) no abre el camino de siempre por otro lado; sin candidatos
    no hay pista."""
    m = _motor(abiertas=frozenset({PLAN, PEDIDO}))
    assert "confirmar_cotizacion" not in _nombres(m)
    assert comandos.INICIAR not in _nombres(m)
    assert m.tarjeta == ""


def test_en_sombra_nada_cambia() -> None:
    m = _motor(modo="sombra")
    assert _nombres(m) == [t.name for t in BASE]
    assert m.tarjeta == ""


# ---------------------------------------------------------------- tarjeta


def test_la_tarjeta_sin_foco_lleva_la_pista_del_kernel() -> None:
    r = tarjeta.rotulos()
    m = _motor()
    assert m.tarjeta.splitlines() == [
        r["encabezado"], r["sin_foco"], r["sin_foco_acciones"].format(acciones="confirmar_cotizacion"),
    ]
    assert "iniciar_procedimiento" in m.tarjeta


def test_con_tareas_en_pausa_la_pista_va_al_final_sin_repetir_el_encabezado() -> None:
    previa = "## Estado de la conversación (x)\nEn pausa: algo"
    salida = tarjeta.pista_sin_foco(previa, ["confirmar_cotizacion", "no valida!"])
    lineas = salida.splitlines()
    assert lineas[:2] == previa.splitlines() and lineas[2] == tarjeta.rotulos()["sin_foco"]
    assert lineas[3].endswith(": confirmar_cotizacion")


def test_sin_puertas_existentes_solo_la_pista() -> None:
    salida = tarjeta.pista_sin_foco("", [])
    assert salida.splitlines() == [tarjeta.rotulos()["encabezado"], tarjeta.rotulos()["sin_foco"]]


def test_los_textos_nuevos_estan_en_los_playbooks_del_kernel() -> None:
    assert "sin_foco" in tarjeta.rotulos() and "sin_foco_acciones" in tarjeta.rotulos()
    assert "{procedimientos}" in mensajes()["sin_recorrido"]


# ---------------------------------------------------------------- negación (lock simulado)


class _Lecturas:
    def __init__(self, filas: list[Any], foco: Any) -> None:
        self.filas = filas
        self.foco = foco
        self.locks = 0
        self.lecturas: list[dict[str, Any]] = []


def _simular(monkeypatch: pytest.MonkeyPatch, filas: list[Any], foco_bajo_lock: Any = None) -> _Lecturas:
    lecturas = _Lecturas(filas, foco_bajo_lock)

    @asynccontextmanager
    async def lock(_t: Any, _c: Any) -> AsyncIterator[Any]:
        lecturas.locks += 1
        yield object()

    async def foco(*_a: Any, **_k: Any) -> Any:
        return lecturas.foco

    async def abiertas(_s: Any, **k: Any) -> list[Any]:
        lecturas.lecturas.append(k)
        return lecturas.filas

    monkeypatch.setattr(ejecucion, "_con_lock", lock)
    monkeypatch.setattr(ejecucion, "_foco", foco)
    monkeypatch.setattr(ejecucion.state, "tareas_abiertas", abiertas)
    return lecturas


async def test_sin_foco_la_puerta_se_niega_con_el_recorrido_a_iniciar(monkeypatch: pytest.MonkeyPatch) -> None:
    lecturas = _simular(monkeypatch, [])
    m = _motor()
    previo = await m.antes_de_accion("confirmar_cotizacion", {"quote_id": "q"})
    assert previo.decision is not None and previo.decision.auditoria == "denied"
    error = previo.decision.resultado["error"]
    assert "iniciar_procedimiento" in error and f"{PLAN}, {PEDIDO}" in error
    assert not previo.gobernada
    # Las tareas abiertas se leen del negocio y la conversación del turno.
    assert lecturas.lecturas == [{"tenant_id": m.tenant_id, "conversation_id": m.conversation_id}]


async def test_un_respaldo_v2_viejo_no_deja_pasar_la_puerta(monkeypatch: pytest.MonkeyPatch) -> None:
    """El turno empezó con una tarea sin gobierno que se cerró a mitad del turno: la decisión se
    relee bajo el lock y la puerta se niega."""
    _simular(monkeypatch, [])
    m = _motor(sin_gobierno=PLAN, abiertas=frozenset({PLAN}))
    assert "confirmar_cotizacion" in _nombres(m)  # la oferta del inicio del turno
    previo = await m.antes_de_accion("confirmar_cotizacion", {})
    assert previo.decision is not None and previo.decision.auditoria == "denied"


async def test_una_tarea_sin_gobierno_de_otro_procedimiento_no_libera_la_puerta(monkeypatch: pytest.MonkeyPatch) -> None:
    _simular(monkeypatch, [types.SimpleNamespace(status="active", stack_position=0,
                                                 procedure_id="pack_calendar.agendar_cita")])
    procs = [_publicado("pack-commerce", "contratar_plan"), _publicado("pack-calendar", "agendar_cita")]
    previo = await _motor(procs=procs).antes_de_accion("confirmar_cotizacion", {})
    assert previo.decision is not None and PLAN in previo.decision.resultado["error"]


async def test_con_foco_la_puerta_del_core_fuera_de_la_etapa_se_niega(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = [_publicado("pack-commerce", "contratar_plan"), _publicado("pack-subscriptions", "cambiar_plan")]
    m = _motor(procs=procs, foco_etapa="datos")
    _simular(monkeypatch, [], foco_bajo_lock=m._foco)
    previo = await m.antes_de_accion("sub_aplicar_cambio_plan", {})
    assert previo.decision is not None and "no corresponde a la etapa" in previo.decision.resultado["error"]


async def test_sin_foco_lo_que_no_es_puerta_pasa_como_siempre(monkeypatch: pytest.MonkeyPatch) -> None:
    lecturas = _simular(monkeypatch, [])
    m = _motor()
    for accion in ("informar_plan", "crear_pedido", "confirmar_pago_pedido", "escalate_to_human"):
        previo = await m.antes_de_accion(accion, {})
        assert previo.decision is None and not previo.gobernada
    assert lecturas.locks == 3  # las del pack miran el foco como siempre; la del core, no


async def test_sin_foco_la_puerta_del_core_se_niega(monkeypatch: pytest.MonkeyPatch) -> None:
    _simular(monkeypatch, [])
    procs = [_publicado("pack-subscriptions", "cambiar_plan")]
    previo = await _motor(procs=procs).antes_de_accion("sub_aplicar_cambio_plan", {})
    assert previo.decision is not None and "pack_subscriptions.cambiar_plan" in previo.decision.resultado["error"]


async def test_con_una_tarea_sin_gobierno_bajo_el_lock_no_se_niega(monkeypatch: pytest.MonkeyPatch) -> None:
    _simular(monkeypatch, [types.SimpleNamespace(status="active", stack_position=0, procedure_id=PLAN)])
    previo = await _motor().antes_de_accion("confirmar_cotizacion", {})
    assert previo.decision is None


async def test_sin_candidatos_se_niega_con_tarea_en_curso(monkeypatch: pytest.MonkeyPatch) -> None:
    pausa = [types.SimpleNamespace(status="suspended", stack_position=1, procedure_id=p) for p in (PLAN, PEDIDO)]
    _simular(monkeypatch, pausa)
    previo = await _motor().antes_de_accion("confirmar_cotizacion", {})
    assert previo.decision is not None and previo.decision.resultado["error"] == mensajes()["tarea_en_curso"]


async def test_en_sombra_no_se_niega_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    lecturas = _simular(monkeypatch, [])
    previo = await _motor(modo="sombra").antes_de_accion("confirmar_cotizacion", {})
    assert previo.decision is None and lecturas.locks == 0

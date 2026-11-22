"""Lint de rótulos: lo que el kernel le muestra al cliente sale con el nombre que publicó la habilidad.

El resumen de confirmación de una acción muestra sus argumentos (`datos_de_args`) y la pregunta
por lo que falta muestra los datos del procedimiento; sin rótulo sale el nombre técnico con
espacios («Start datetime», «Attendee email»: chat web de Estudio Aurora, 2026-11-19). Este test
exige un rótulo para cada uno, salvo lo que el kernel nunca muestra (ids, credenciales, datos de
pago) y las acciones cuyo resumen arma la habilidad (`register_resumen`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from core.agents.credenciales import es_sensible
from core.kernel.confirmacion import _rotulo
from core.kernel.procedures import motor
from core.mcp.host import MCPHost


def _calendar(host: MCPHost) -> None:
    from pack_calendar.procedimientos import registrar
    from pack_calendar.skills import CalendarSkill

    host.register_skill(CalendarSkill())
    registrar(host, "pack_calendar")


def _cobranza(host: MCPHost) -> None:
    from pack_cobranza.procedimientos import registrar
    from pack_cobranza.skills import CobranzaSkill

    host.register_skill(CobranzaSkill())
    registrar(host, "pack_cobranza")


def _commerce(host: MCPHost) -> None:
    from pack_commerce.procedimientos import registrar
    from pack_commerce.skills import CatalogSkill, OrderSkill

    host.register_skill(CatalogSkill())
    host.register_skill(OrderSkill())
    registrar(host)


def _subscriptions(host: MCPHost) -> None:
    from pack_subscriptions.procedimientos import registrar

    registrar(host, "pack_subscriptions")  # sus tools `sub_*` son del core


PACKS: dict[str, Callable[[MCPHost], None]] = {
    "pack_calendar": _calendar,
    "pack_cobranza": _cobranza,
    "pack_commerce": _commerce,
    "pack_subscriptions": _subscriptions,
}


@pytest.fixture(scope="module")
def host() -> MCPHost:
    from core.tools.core_skill import register_core_skills

    h = MCPHost()
    register_core_skills(h)  # como en producción: las del core antes que los packs
    for pack_id, activar in PACKS.items():
        h.begin_activation(pack_id)
        activar(h)
        h.end_activation()
    return h


def _visible(nombre: str, pagos: frozenset[str]) -> bool:
    # `_rotulo` sin rótulos devuelve None para lo que nunca se muestra (ids).
    return _rotulo(nombre) is not None and not nombre.startswith("_") and not es_sensible(nombre) and nombre not in pagos


def _faltantes(host: MCPHost, pack_id: str) -> list[str]:
    rotulos = host.get_rotulos(pack_id)
    faltan: list[str] = []
    for proc in host.get_procedures().get(pack_id, []):
        pagos = frozenset(n for n, s in proc.slots.items() if s.dataClass == "payment")
        for nombre, slot in proc.slots.items():
            # Lo que llena el servidor no se le pregunta al cliente.
            if slot.setBy is None and _visible(nombre, pagos) and nombre not in rotulos:
                faltan.append(f"{proc.id}: dato {nombre}")
        for stage in proc.stages:
            for accion in stage.actions:
                if not motor.requiere_confirmacion(stage, accion) or host.get_resumen(accion) is not None:
                    continue
                tool = host.get_tool(accion)
                propiedades: dict[str, Any] = ((tool.parameters if tool is not None else {}) or {}).get("properties") or {}
                faltan.extend(
                    f"{proc.id}: argumento {n} de {accion}"
                    for n in propiedades
                    if _visible(str(n), pagos) and str(n) not in rotulos
                )
    return sorted(set(faltan))


@pytest.mark.parametrize("pack_id", sorted(PACKS))
def test_todo_lo_que_ve_el_cliente_tiene_rotulo(host: MCPHost, pack_id: str) -> None:
    assert host.get_procedures().get(pack_id), f"{pack_id} no registró procedimientos"
    assert _faltantes(host, pack_id) == []


def test_la_confirmacion_de_agenda_sale_en_espanol(host: MCPHost) -> None:
    """El caso de Estudio Aurora: el resumen de `create_calendar_event` con los rótulos del pack."""
    from core.kernel.confirmacion import armar, datos_de_args

    tool = host.get_tool("create_calendar_event")
    assert tool is not None
    args = {
        "title": "Masaje descontracturante con Sebastián Correa",
        "start_datetime": "2026-11-23T15:00:00-05:00",
        "description": "Masaje descontracturante solicitado por el cliente.",
        "attendee_email": "sebastian.correa@ejemplo.test",
        "duration_minutes": 60,
        "appointment_type_id": "17519655-56fc-4ae8-8811-72d4db895cbb",
    }
    datos = datos_de_args(args, list(tool.parameters["properties"]))
    cuerpo = armar("Agendar una cita", datos, "x", rotulos=host.get_rotulos("pack_calendar")).texto
    for rotulo in ("Cita:", "Fecha y hora:", "Detalle:", "Correo:", "Duración (minutos):"):
        assert rotulo in cuerpo
    for tecnico in ("Title", "Start datetime", "Attendee email", "Duration minutes", "Appointment type"):
        assert tecnico not in cuerpo


def test_un_rotulo_de_un_argumento_ajeno_se_rechaza() -> None:
    """Los argumentos aceptados son los de las acciones de SUS procedimientos, no cualquier nombre."""
    from pack_calendar.procedimientos import registrar
    from pack_calendar.skills import CalendarSkill

    from core.shared.exceptions import PluginError

    h = MCPHost()
    h.begin_activation("pack_calendar")
    h.register_skill(CalendarSkill())
    registrar(h, "pack_calendar")
    h.register_rotulos({"days": "Días"})  # argumento de find_next_available, acción de una etapa
    with pytest.raises(PluginError, match="Rótulo inválido: cancel_all"):
        h.register_rotulos({"cancel_all": "Todas"})  # de cancel_calendar_event: no está en ninguna etapa
    h.end_activation()


# ---------------------------------------------------------------- argumentos atados a los datos
def _agendar_cita(host: MCPHost) -> Any:
    return next(p for p in host.get_procedures()["pack_calendar"] if p.id == "pack_calendar.agendar_cita")


def _tarea(slots: dict[str, Any]) -> Any:
    from datetime import UTC, datetime

    from core.kernel.state import Proyeccion

    t0 = datetime(2026, 11, 19, tzinfo=UTC)
    return Proyeccion(
        procedure_id="pack_calendar.agendar_cita", procedure_version="1.2.0", status="active", stage="agendar",
        stack_position=0, last_seq=len(slots) + 1, created_at=t0, updated_at=t0,
        slots={
            n: {"value": v, "source": "llm", "seq": i, "confirmed": False, "data_class": "public"}
            for i, (n, v) in enumerate(slots.items(), start=2)
        },
    )


def test_el_correo_de_la_cita_es_el_del_dato_no_el_que_reescribe_el_modelo(host: MCPHost) -> None:
    """Sebastián dio `sebastian.correa@…` (quedó en el dato) y el modelo pasó `sebastian.correo@…`
    a `create_calendar_event`: con los datos llamados como los argumentos, el motor lo detecta y
    la acción se niega (`argumento_ligado`) en vez de pedir confirmar un correo que el cliente no dio."""
    tool = host.get_tool("create_calendar_event")
    assert tool is not None
    tarea = _tarea({"start_datetime": "2026-11-23T15:00:00-05:00", "attendee_email": "sebastian.correa@ejemplo.test"})
    args = {"title": "Masaje", "start_datetime": "2026-11-23T15:00:00-05:00", "attendee_email": "sebastian.correo@ejemplo.test"}
    lig = motor.ligar_argumentos(_agendar_cita(host), tarea, tool.parameters, args)
    assert [d.dato for d in lig.distintos] == ["attendee_email"]

    igual = motor.ligar_argumentos(
        _agendar_cita(host), tarea, tool.parameters, {**args, "attendee_email": " Sebastian.Correa@ejemplo.test "}
    )
    assert igual.distintos == ()
    assert igual.args["attendee_email"] == "sebastian.correa@ejemplo.test"  # canónico: el hash no cambia


def test_sin_correo_en_los_argumentos_se_completa_con_el_del_dato(host: MCPHost) -> None:
    tool = host.get_tool("create_calendar_event")
    assert tool is not None
    tarea = _tarea({"start_datetime": "2026-11-24T10:00:00-05:00", "attendee_email": "felipe.ocampo@ejemplo.test"})
    lig = motor.ligar_argumentos(_agendar_cita(host), tarea, tool.parameters, {"title": "Limpieza facial"})
    # `start_datetime` es obligatorio en la tool: se completa; el correo es opcional en el schema.
    assert lig.args["start_datetime"] == "2026-11-24T10:00:00-05:00"
    assert lig.distintos == ()

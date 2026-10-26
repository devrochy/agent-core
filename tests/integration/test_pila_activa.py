"""HU-AOS-20 parte 2 · la pila de tareas activa con el loop real y un modelo guionado (sin LLM).

Negocio v3 con Ventas (por defecto, `contratar_plan`) y Agenda (`agendar_cita`), ruteo y pila en
`active`:

- **Dos pedidos:** «quiero el plan premium y una demo el jueves» abre el plan en foco (Ventas) y la
  demo en pausa con su dueño (Agenda); la tarjeta trae la guía de una sola pregunta.
- **Retomar:** con el plan cerrado fuera del turno, el siguiente turno retoma la demo y lo atiende
  Agenda (`source = task`, dueño de la conversación).
- **Cancelar en el turno:** la demo de Agenda se retoma, pero el resto del turno Ventas no ofrece sus
  acciones y la tarjeta dice que la sigue Agenda.
- **Digresión:** con el plan en foco, «… y también una demo» la encola en pausa.
- **Sobrantes:** tres pedidos con `maxStack = 2`: el tercero no se abre y va a la tarjeta.
- **`maxStack = 1`:** no hay pila.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from test_enrutamiento_especialistas import (
    CITA,
    PLAN,
    _con_dos_especialistas,
    _conversacion,
    _decision,
    _dueno,
    _especialista,
    _Guion,
    _mensaje,
    _modules,
    _publicar_release,
    _sql,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PEDIDO = "pack_commerce.tomar_pedido"
DOS = "quiero el plan premium y una demo el jueves"


@pytest.fixture
def _ruteo(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


class _GuionConPrompt(_Guion):
    def __init__(self, pasos: list[Any]) -> None:
        super().__init__(pasos)
        self.prompts: list[str] = []

    async def generate(self, **kwargs: Any) -> Any:
        bloques = kwargs.get("system_blocks") or []
        self.prompts.append("\n".join(str(getattr(b, "text", b)) for b in bloques))
        return await super().generate(**kwargs)


async def _turno(tid: uuid.UUID, conv: uuid.UUID, monkeypatch: Any, pasos: list[Any], texto: str) -> _GuionConPrompt:
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    guion = _GuionConPrompt(pasos)
    await _mensaje(tid, conv)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    async with tenant_scope(tid):
        await process_conversation(conv, texto)
    await _mensaje(tid, conv, direccion="outbound")
    return guion


async def _pila_activa(tid: uuid.UUID) -> None:
    from core.kernel import banderas_pila
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await banderas_pila.fijar(tid, {"pila": "active"}, actor="test")


async def _kernelspec(tid: uuid.UUID, cid: uuid.UUID, **campos: Any) -> None:
    """Publica el Coordinador con estos campos del KernelSpec y vuelve a publicar la release."""
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    ((aid, contenido),) = await _sql(
        tid,
        "select a.id, v.content from agents a join agent_versions v on v.tenant_id = a.tenant_id and v.agent_id = a.id "
        "where a.key = 'coordinador' order by v.version desc limit 1",
    )
    async with tenant_scope(tid):
        await versiones.publicar(tid, aid, catalogo=catalogo_de_acciones(_modules()), contenido={**dict(contenido), **campos})
    await _publicar_release(tid, cid)


async def _tareas(tid: uuid.UUID, conv: uuid.UUID) -> dict[str, tuple[str, int, uuid.UUID | None, uuid.UUID]]:
    filas = await _sql(
        tid,
        "select procedure_id, status, stack_position, owner_instance_id, id from conversation_tasks "
        "where conversation_id = :c and parent_task_id is null",
        c=conv,
    )
    return {str(f[0]): (str(f[1]), int(f[2]), f[3], f[4]) for f in filas}


async def _cerrar(tid: uuid.UUID, conv: uuid.UUID, task_id: uuid.UUID) -> None:
    """Completa una tarea fuera del turno (como un cierre por evento): no retoma nada todavía."""
    from core.db.session import get_session
    from core.kernel import state
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            await state.registrar(
                s, tenant_id=tid, conversation_id=conv, task_id=task_id, tipo="completed", data={"reason": "test"},
                actor="kernel",
            )


async def _con_pila() -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    tid, cid, inst = await _con_dos_especialistas("active")
    await _pila_activa(tid)
    return tid, cid, inst


# ---------------------------------------------------------------- dos pedidos y retomar
async def test_dos_pedidos_abren_foco_y_pausa_con_su_dueno(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_pila()
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, ["El Premium cuesta lo que ves; la demo la vemos después. ¿Mensual o anual?"], texto=DOS)
    tareas = await _tareas(tid, conv)
    assert tareas[PLAN][:3] == ("active", 0, inst["ventas"])
    assert tareas[CITA][:3] == ("suspended", 1, inst["agenda"])
    d = await _decision(tid, conv)
    assert d["route_source"] == "trigger" and d["active_instance_id"] == str(inst["ventas"])
    assert "pregunta solo por la tarea en foco" in g.prompts[0] and "En pausa:" in g.prompts[0]


async def test_al_cerrar_el_foco_se_retoma_la_pausa_y_atiende_su_dueno(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_pila()
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, ["¿Mensual o anual?"], texto=DOS)
    await _cerrar(tid, conv, (await _tareas(tid, conv))[PLAN][3])
    g = await _turno(tid, conv, monkeypatch, ["¿Qué día te sirve?"], texto="listo, gracias")
    tareas = await _tareas(tid, conv)
    assert tareas[CITA][:2] == ("active", 0)
    d = await _decision(tid, conv)
    assert d["route_source"] == "task" and d["active_instance_id"] == str(inst["agenda"])
    assert await _dueno(tid, conv) == inst["agenda"]
    assert "check_availability" in g.tools[0]


async def test_cancelar_en_el_turno_retoma_la_de_otro_sin_sus_acciones(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_pila()
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, ["¿Mensual o anual?"], texto=DOS)
    g = await _turno(
        tid, conv, monkeypatch,
        [
            ("cancelar_tarea", {}),
            # Aunque no se ofrecen, llamarlas por nombre tampoco sirve (la tarea es de Agenda).
            ("check_availability", {"date": "2030-01-10"}),
            ("actualizar_tarea", {"slots": {"fecha": "2030-01-10"}}),
            "Listo, la demo la sigue Agenda.",
        ],
        texto="mejor no quiero el plan",
    )
    tareas = await _tareas(tid, conv)
    assert tareas[PLAN][0] == "cancelled" and tareas[CITA][:2] == ("active", 0)
    # Después de cancelar, en el mismo turno: la demo es de Agenda, Ventas no ofrece sus acciones.
    assert "check_availability" not in g.tools[1]
    negadas = g.resultados[1:3]
    assert len(negadas) == 2 and all("error" in r.lower() or "no " in r.lower() for r in negadas), negadas
    eventos = await _sql(
        tid, "select count(*) from task_events where task_id = :t and type = 'slot_set'", t=tareas[CITA][3]
    )
    assert eventos == [(0,)]
    assert "Esta tarea la sigue" in g.prompts[1]
    assert await _dueno(tid, conv) == inst["ventas"]  # el cambio lo hace el router en el turno siguiente
    await _turno(tid, conv, monkeypatch, ["¿Qué día?"], texto="ok")
    assert await _dueno(tid, conv) == inst["agenda"]


async def test_digresion_con_foco_encola_en_pausa(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_pila()
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, [("iniciar_procedimiento", {"procedure_id": PLAN}), "¿Qué plan?"], texto="quiero el plan premium")
    await _turno(tid, conv, monkeypatch, ["Anotado."], texto="el premium, y también quiero agendar una demo")
    tareas = await _tareas(tid, conv)
    assert tareas[PLAN][:2] == ("active", 0) and tareas[CITA][:3] == ("suspended", 1, inst["agenda"])
    d = await _decision(tid, conv)
    assert d["route_source"] == "task" and d["active_instance_id"] == str(inst["ventas"])


# ---------------------------------------------------------------- tope de la pila
async def test_tres_pedidos_con_max_stack_2_anota_el_sobrante(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_dos_especialistas("active")
    await _especialista(tid, cid, "pedidos", PEDIDO, {"consultar_inventario", "crear_pedido"}, defecto=False)
    await _publicar_release(tid, cid)
    await _pila_activa(tid)
    conv = await _conversacion(tid, cid)
    g = await _turno(
        tid, conv, monkeypatch, ["¿Mensual o anual?"],
        texto="quiero el plan premium, también una demo el jueves y además pedir dos hamburguesas a domicilio",
    )
    tareas = await _tareas(tid, conv)
    assert set(tareas) == {PLAN, CITA}, tareas
    assert "El cliente también pidió" in g.prompts[0]


async def test_max_stack_1_apaga_la_pila(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_pila()
    await _kernelspec(tid, cid, procedures={"maxStack": 1})
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, ["¿En qué te ayudo?"], texto=DOS)
    assert await _tareas(tid, conv) == {}

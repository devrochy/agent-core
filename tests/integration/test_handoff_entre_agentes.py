"""HU-AOS-19 parte 1 · handoff en dos fases con el loop real y un modelo guionado (sin LLM).

Negocio `aos_runtime = v3` con el motor y `pack_commerce`, dos especialistas que declaran
`contratar_plan` (Ventas por defecto y Soporte) y Agenda, que solo declara `agendar_cita`:

- **Sin pérdida:** Ventas, con una tarea a medias, pasa a Soporte. Al cerrar el turno, el dueño de la
  conversación y de la tarea es Soporte, los datos siguen igual, hay `owner_changed`, el ledger dice
  `handoff_applied` y `turn_outcomes.final_instance_id` = Soporte. El turno siguiente lo atiende
  Soporte con la misma tarea, con el ruteo apagado y activo (`source = task`).
- **Contrato expuesto:** Agenda no declara la tarea abierta, así que no se ofrece.
- **Ping-pong:** Ventas → Soporte → Ventas pasa; el tercer pedido sale `owner_frozen`.
- **Fase 2 que no aplica:** con un turno escalado, o con el dueño cambiado por otro turno, el dueño
  no cambia y el ledger dice `cancelled`.
- **Sin grafo:** no se ofrece `pasar_a_agente`.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from test_enrutamiento_especialistas import (
    CITA,
    PLAN,
    _conversacion,
    _decision,
    _dueno,
    _especialista,
    _modo,
    _modules,
    _negocio,
    _publicar_release,
    _sql,
    _turno,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASAR = "pasar_a_agente"


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.tools.core_skill import register_core_skills

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    await plugin_manager.ensure_process_activation()
    register_core_skills(mcp_host)


@pytest.fixture
def _ruteo(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _equipo(modo: str, enlaces: list[tuple[str, str]] | None) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID], dict[str, uuid.UUID]]:
    """(tid, conexión, instancias por key, agentes por key). `enlaces`: pares con `handoff`."""
    from core.agents import grafo_a2a
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio()
    agentes = {
        "ventas": await _especialista(tid, cid, "ventas", PLAN, {"informar_plan"}, defecto=True),
        "soporte": await _especialista(tid, cid, "soporte", PLAN, {"informar_plan"}, defecto=False),
        "agenda": await _especialista(tid, cid, "agenda", CITA, {"list_appointment_types"}, defecto=False),
    }
    if enlaces is not None:
        async with tenant_scope(tid):
            await grafo_a2a.publicar(
                tid,
                [
                    {"from_agent_id": str(agentes[a]), "to_agent_id": str(agentes[b]), "modes": ["handoff"]}
                    for a, b in enlaces
                ],
            )
    await _publicar_release(tid, cid)
    await _modo(tid, modo)
    filas = await _sql(
        tid,
        "select a.key, i.id from agent_instances i join agents a on a.tenant_id = i.tenant_id and a.id = i.agent_id "
        "where i.connection_id = :c and i.deleted_at is null",
        c=cid,
    )
    return tid, cid, {k: i for k, i in filas}, agentes


async def _tarea_con_dato(tid: uuid.UUID, conv: uuid.UUID, monkeypatch: Any) -> uuid.UUID:
    """Ventas inicia `contratar_plan` y el equipo fija `business_name` (dato del recorrido)."""
    from core.db.session import get_session
    from core.kernel import state
    from core.shared.tenant import tenant_scope

    await _turno(tid, conv, monkeypatch, [("iniciar_procedimiento", {"procedure_id": PLAN}), "¿Qué plan?"], texto="quiero un plan")
    ((task_id,),) = await _sql(tid, "select id from conversation_tasks where conversation_id = :c and status = 'active'", c=conv)
    async with tenant_scope(tid):
        async with get_session() as s:
            await state.registrar(
                s, tenant_id=tid, conversation_id=conv, task_id=task_id, tipo="slot_set",
                data={"slot": "business_name", "value": "Ferretería Luna", "source": "human", "data_class": "public"},
                actor="human",
            )
    return task_id


async def _ledger(tid: uuid.UUID, conv: uuid.UUID) -> list[tuple[str, str | None, str]]:
    return await _sql(
        tid,
        "select e.type, e.reason, c.status from a2a_events e join a2a_conversations c "
        "on c.tenant_id = e.tenant_id and c.id = e.a2a_conversation_id where c.conversation_id = :c "
        "order by c.started_at, e.seq",
        c=conv,
    )


# ---------------------------------------------------------------- sin pérdida
@pytest.mark.parametrize("modo", ["off", "active"])
async def test_handoff_sin_perdida(_ruteo: None, monkeypatch: Any, modo: str) -> None:
    tid, cid, inst, _ = await _equipo(modo, [("ventas", "soporte")])
    conv = await _conversacion(tid, cid)
    task_id = await _tarea_con_dato(tid, conv, monkeypatch)

    g = await _turno(tid, conv, monkeypatch, [(PASAR, {"agente": "soporte"}), "Te paso con soporte."], texto="tengo un problema")
    assert PASAR in g.tools[0]
    assert '"ok": true' in g.resultados[0].lower() and "soporte" in g.resultados[0]
    assert await _dueno(tid, conv) == inst["soporte"]
    ((dueno_tarea, slots, etapa),) = await _sql(
        tid, "select owner_instance_id, slots, stage from conversation_tasks where id = :t", t=task_id
    )
    assert dueno_tarea == inst["soporte"] and slots["business_name"]["value"] == "Ferretería Luna" and etapa == "elegir_plan"
    ((datos,),) = await _sql(tid, "select data from task_events where task_id = :t and type = 'owner_changed'", t=task_id)
    assert {k: datos[k] for k in ("from_instance_id", "to_instance_id")} == {
        "from_instance_id": str(inst["ventas"]),
        "to_instance_id": str(inst["soporte"]),
    }
    assert [(t, s) for t, _r, s in await _ledger(tid, conv)] == [("accepted", "completed"), ("handoff_applied", "completed")]
    ((atendio, termino),) = await _sql(
        tid,
        "select d.active_instance_id, o.final_instance_id from core_decisions d join turn_outcomes o "
        "on o.tenant_id = d.tenant_id and o.turn_id = d.id where d.conversation_id = :c order by d.created_at desc limit 1",
        c=conv,
    )
    assert (atendio, termino) == (inst["ventas"], inst["soporte"])

    # El turno siguiente lo atiende Soporte con la misma tarea en foco.
    g = await _turno(tid, conv, monkeypatch, ["Sigo con tu plan."], texto="¿seguimos?")
    d = await _decision(tid, conv)
    assert d["active_instance_id"] == str(inst["soporte"]) and d["focus_task_id"] == str(task_id)
    if modo == "active":
        assert d["route_source"] == "task"
    assert "actualizar_tarea" in g.tools[0] and PASAR not in g.tools[0]  # Soporte no tiene enlaces


# ---------------------------------------------------------------- contrato expuesto y sin grafo
async def test_no_se_ofrece_un_destino_que_no_declara_la_tarea(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst, _ = await _equipo("off", [("ventas", "agenda")])
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, ["Hola."], texto="hola")
    assert PASAR in g.tools[0]  # sin tareas abiertas, Agenda sí es destino
    await _tarea_con_dato(tid, conv, monkeypatch)
    g = await _turno(tid, conv, monkeypatch, [(PASAR, {"agente": "agenda"}), "Sigo yo."], texto="agenda")
    assert PASAR not in g.tools[0]
    assert '"ok": false' in g.resultados[0].lower()
    assert await _dueno(tid, conv) == inst["ventas"]
    assert await _ledger(tid, conv) == []


async def test_sin_grafo_no_se_ofrece(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst, _ = await _equipo("off", None)
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, ["Hola."], texto="hola")
    assert PASAR not in g.tools[0]


# ---------------------------------------------------------------- ping-pong
async def test_ping_pong_se_congela_en_el_tercer_cambio(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst, _ = await _equipo("off", [("ventas", "soporte"), ("soporte", "ventas")])
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, [(PASAR, {"agente": "soporte"}), "Te paso."], texto="uno")
    assert await _dueno(tid, conv) == inst["soporte"]
    await _turno(tid, conv, monkeypatch, [(PASAR, {"agente": "ventas"}), "Te devuelvo."], texto="dos")
    assert await _dueno(tid, conv) == inst["ventas"]
    g = await _turno(tid, conv, monkeypatch, [(PASAR, {"agente": "soporte"}), "Sigo yo."], texto="tres")
    assert '"ok": false' in g.resultados[0].lower()
    assert await _dueno(tid, conv) == inst["ventas"]
    razones = [r for t, r, _s in await _ledger(tid, conv) if t == "denied"]
    assert razones == ["owner_frozen"]


# ---------------------------------------------------------------- fase 2 que no aplica
async def _pendiente(tid: uuid.UUID, cid: uuid.UUID, conv: uuid.UUID, inst: dict[str, uuid.UUID], status: str) -> Any:
    """Un turno con su cabecera y un handoff aceptado en la fase 1, sin cerrar."""
    from core.kernel import a2a, decisions, handoff
    from core.shared.tenant import tenant_scope

    rid = (await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid))[0][0]
    turno_id = uuid.uuid4()
    await _sql(
        tid,
        "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_id, outcome, latency_ms) "
        "values (:id, :t, :cc, :conv, :r, 'routed', 5)",
        id=turno_id, t=tid, cc=cid, conv=conv, r=rid,
    )
    await _sql(tid, "update conversations set active_agent_instance_id = :v where id = :c", v=inst["ventas"], c=conv)
    estado = a2a.EstadoDelTurno(tid, cid, conv, turno_id, release_id=rid)
    grafo = a2a.GrafoCongelado.desde_manifiesto(
        {"links": [{"from_agent_id": "00000000-0000-0000-0000-000000000001", "to_agent_id": "00000000-0000-0000-0000-000000000002", "modes": ["handoff"]}]},
        {inst["ventas"]: uuid.UUID(int=1), inst["soporte"]: uuid.UUID(int=2)},
    )
    async with tenant_scope(tid):
        d = await a2a.registrar(estado, a2a.Operacion(inst["ventas"], inst["soporte"], "handoff", "handoff"), grafo, a2a.Limites())
    assert d.permitido
    a2a.liberar(estado, d.call_id)
    turno = decisions.Turno(id=turno_id, tenant_id=tid, t0=0.0, conversation_id=conv, decidido=True, status=status)
    turno.a2a = estado
    turno.handoff = handoff.Pendiente(estado, d.call_id, inst["ventas"], inst["soporte"], conv)
    return turno


async def test_un_turno_escalado_no_cambia_de_dueno(monkeypatch: Any) -> None:
    from core.kernel import handoff
    from core.shared.tenant import tenant_scope

    tid, cid, inst, _ = await _equipo("off", [("ventas", "soporte")])
    conv = await _conversacion(tid, cid)
    turno = await _pendiente(tid, cid, conv, inst, "escalated")
    async with tenant_scope(tid):
        assert await handoff.cerrar(turno) is None
    assert await _dueno(tid, conv) == inst["ventas"]
    assert [(t, s) for t, _r, s in await _ledger(tid, conv)] == [("accepted", "failed"), ("cancelled", "failed")]


async def test_si_otro_turno_cambio_el_dueno_se_cancela(monkeypatch: Any) -> None:
    from core.kernel import handoff
    from core.shared.tenant import tenant_scope

    tid, cid, inst, _ = await _equipo("off", [("ventas", "soporte")])
    conv = await _conversacion(tid, cid)
    turno = await _pendiente(tid, cid, conv, inst, "replied")
    await _sql(tid, "update conversations set active_agent_instance_id = :a where id = :c", a=inst["agenda"], c=conv)
    async with tenant_scope(tid):
        assert await handoff.cerrar(turno) is None
    assert await _dueno(tid, conv) == inst["agenda"]
    assert [t for t, _r, _s in await _ledger(tid, conv)] == ["accepted", "cancelled"]


async def test_fuera_del_negocio_no_aplica(monkeypatch: Any) -> None:
    from core.kernel import handoff
    from core.shared.tenant import tenant_scope

    tid, cid, inst, _ = await _equipo("off", [("ventas", "soporte")])
    otro, _ = await _negocio()
    conv = await _conversacion(tid, cid)
    turno = await _pendiente(tid, cid, conv, inst, "replied")
    async with tenant_scope(otro):
        assert await handoff.cerrar(turno) is None
    assert await _dueno(tid, conv) == inst["ventas"]


async def test_una_tarea_abierta_despues_de_aceptar_cancela_el_handoff(monkeypatch: Any) -> None:
    from core.db.session import get_session
    from core.kernel import handoff, state
    from core.shared.tenant import tenant_scope

    tid, cid, inst, _ = await _equipo("off", [("ventas", "soporte")])
    conv = await _conversacion(tid, cid)
    turno = await _pendiente(tid, cid, conv, inst, "replied")  # aceptado sin tareas abiertas
    async with tenant_scope(tid):
        async with get_session() as s:
            await state.iniciar(
                s, tenant_id=tid, conversation_id=conv, procedure_id=CITA, procedure_version="1.0.0",
                stage="inicio", actor="kernel", owner_instance_id=inst["ventas"],
            )
        assert await handoff.cerrar(turno) is None
    assert await _dueno(tid, conv) == inst["ventas"]
    assert [t for t, _r, _s in await _ledger(tid, conv)] == ["accepted", "cancelled"]
    tareas = await _sql(tid, "select owner_instance_id from conversation_tasks where conversation_id = :c", c=conv)
    assert tareas == [(inst["ventas"],)]  # la tarea nueva no cambió de dueño

"""HU-AOS-19 parte 2 · encargo y consulta entre agentes con el motor y la base reales (sin LLM).

El contexto A2A se arma a mano: ningún procedimiento de los packs es encargable (todos tienen
acción, confirmación o precondición), así que el destino expone un procedimiento de datos de prueba
(`pack_calendar.reservar_demo`), como dice la adenda HU-AOS-19 §4.

- **Encargo que se completa:** la sub-tarea cuelga de la tarea en foco, la atiende el destino,
  queda `completed` sin LLM y vuelve el resultado tipado. En el ledger: `accepted` y `completed`.
- **Encargo incompleto / dato no dicho:** se deshace todo (no queda sub-tarea), vuelve qué falta y
  el ledger escribe `failed`.
- **Permiso sin `pii`:** el encargo con un dato personal se niega (`data_class`).
- **Consulta:** corre con la identidad del destino y una clave idempotente, y la salida se valida
  contra el schema. Si no valida, `failed`. Una acción que gobierna el motor no se ofrece.
"""

from __future__ import annotations

import copy
import os
import uuid
from typing import Any

import pytest
from agentcore_contracts import Procedure
from test_enrutamiento_especialistas import (
    CITA,
    PLAN,
    _conversacion,
    _especialista,
    _modules,
    _negocio,
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

DEMO = Procedure.model_validate(
    {
        "contract": "procedure/1",
        "id": "pack_calendar.reservar_demo",
        "version": "1.0.0",
        "title": "Reservar una demo",
        "triggers": {"description": "Reservar una demo", "examples": ["quiero una demo"]},
        "slots": {"fecha": {"type": "date"}, "hora": {"type": "time"}, "nombre": {"type": "string", "dataClass": "pii"}},
        "stages": [
            {"id": "datos", "goal": "Tomar los datos", "exitWhen": {"slots": ["fecha", "hora", "nombre"]}},
            {"id": "listo", "goal": "Listo"},
        ],
        "recovery": {"actionError": "explicar_y_reintentar_una_vez", "timeout": "relevo_humano"},
    }
)
CONSULTA = {
    "id": "agenda.tipos",
    "description": "Tipos de cita",
    "action": "list_appointment_types",
    "outputSchema": {"type": "object", "properties": {"estado": {"type": "string"}}, "required": ["estado"]},
}


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


def _congelado(key: str, instancia: uuid.UUID, agente: uuid.UUID, content: dict[str, Any]) -> Any:
    from core.shared.release_vigente import AgenteCongelado

    return AgenteCongelado(
        instance_id=instancia, agent_id=agente, key=key, role="specialist", version_id=uuid.uuid4(),
        version=1, contract="agentspec/3", content=content, procedimientos=(),
    )


@pytest.fixture(autouse=True)
def _lectura(monkeypatch: Any) -> None:
    """La acción de prueba de la consulta, marcada como de solo lectura."""
    from core.mcp.host import mcp_host

    tool = mcp_host.get_tool("list_appointment_types")
    assert tool is not None
    monkeypatch.setattr(tool, "solo_lectura", True)


async def _escenario(*, clases: list[str], gobernadas: list[str] | None = None) -> dict[str, Any]:
    """Negocio con Ventas (tarea de `contratar_plan` en foco) y Agenda; enlace delegate+query."""
    from core.db.session import get_session
    from core.kernel import a2a, state
    from core.kernel.handoff import ContextoA2A
    from core.kernel.procedures.ejecucion import ContextoMotor, _foco
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio()
    ventas_a = await _especialista(tid, cid, "ventas", PLAN, {"informar_plan"}, defecto=True)
    agenda_a = await _especialista(tid, cid, "agenda", CITA, {"list_appointment_types"}, defecto=False)
    await _publicar_release(tid, cid)
    filas = await _sql(
        tid,
        "select a.key, i.id from agent_instances i join agents a on a.tenant_id = i.tenant_id and a.id = i.agent_id "
        "where i.connection_id = :c and i.deleted_at is null",
        c=cid,
    )
    inst = dict(filas)
    conv = await _conversacion(tid, cid)
    ((cliente,),) = await _sql(tid, "select customer_id from conversations where id = :c", c=conv)
    await _sql(
        tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content) "
        "values (:id, :t, :c, 'inbound', 'customer', 'soy Ana Pérez, quiero una demo')",
        id=uuid.uuid4(), t=tid, c=conv,
    )
    turno = uuid.uuid4()
    rid = (await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid))[0][0]
    await _sql(
        tid,
        "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_id, outcome, latency_ms) "
        "values (:id, :t, :cc, :conv, :r, 'routed', 5)",
        id=turno, t=tid, cc=cid, conv=conv, r=rid,
    )
    permitidos = frozenset({"pack_commerce", "pack_calendar", "core"})
    async with tenant_scope(tid):
        async with get_session() as s:
            await state.iniciar(
                s, tenant_id=tid, conversation_id=conv, procedure_id=PLAN, procedure_version="1.3.0",
                stage="elegir_plan", actor="kernel", owner_instance_id=inst["ventas"],
            )
        async with get_session() as s:
            foco = await _foco(s, tid, conv, permitidos)
    assert foco is not None
    ventas = _congelado("ventas", inst["ventas"], ventas_a, {})
    agenda = _congelado(
        "agenda", inst["agenda"], agenda_a,
        {"tools": [{"tool": "list_appointment_types"}], "exposes": {"procedures": [DEMO.id], "consultas": [CONSULTA]}},
    )
    grafo = a2a.GrafoCongelado.desde_manifiesto(
        {"links": [{"from_agent_id": str(ventas_a), "to_agent_id": str(agenda_a), "modes": ["delegate", "query"], "data_classes": clases}]},
        {inst["ventas"]: ventas_a, inst["agenda"]: agenda_a},
    )
    procs: dict[str, Procedure] = {}
    if gobernadas:
        datos = copy.deepcopy(DEMO.model_dump(mode="json", by_alias=True, exclude_none=True))
        datos["id"], datos["stages"][0]["actions"] = "pack_calendar.otro", gobernadas
        procs["pack_calendar.otro"] = Procedure.model_validate(datos)
    ctx = ContextoA2A(
        grafo, a2a.Limites(), a2a.EstadoDelTurno(tid, cid, conv, turno, release_id=rid), ventas, (ventas, agenda),
        procedimientos_por_agente={inst["agenda"]: {DEMO.id: DEMO}}, procedimientos=procs,
        packs=frozenset({"pack_commerce", "pack_calendar"}),
    )
    motor = ContextoMotor(
        modo="activo", tenant_id=tid, conversation_id=conv, customer_id=cliente, channel="instagram",
        procedimientos={}, herramientas_base=[], candidatas=[], foco=foco, permitidos=permitidos, a2a=ctx,
    )
    llamadas: list[tuple[str, str, dict[str, Any], str]] = []

    async def _ejecutor(destino: Any, accion: str, args: dict[str, Any], clave: str) -> dict[str, Any]:
        llamadas.append((destino.key, accion, args, clave))
        return dict(motor_respuesta)

    motor_respuesta: dict[str, Any] = {"estado": "activa"}
    motor.ejecutor_de_consulta = _ejecutor
    return {"tid": tid, "conv": conv, "motor": motor, "inst": inst, "foco": foco, "llamadas": llamadas, "respuesta": motor_respuesta}


async def _ledger(tid: uuid.UUID, conv: uuid.UUID) -> list[tuple[str, str | None]]:
    return await _sql(
        tid,
        "select e.type, e.reason from a2a_events e join a2a_conversations c on c.tenant_id = e.tenant_id "
        "and c.id = e.a2a_conversation_id where c.conversation_id = :c order by e.seq",
        c=conv,
    )


async def _comando(e: dict[str, Any], nombre: str, args: dict[str, Any]) -> dict[str, Any]:
    from core.shared.tenant import tenant_scope

    async with tenant_scope(e["tid"]):
        return await e["motor"].ejecutar_comando(nombre, args)


async def _subtareas(e: dict[str, Any]) -> list[Any]:
    return await _sql(
        e["tid"],
        "select procedure_id, status, owner_instance_id, parent_task_id, slots from conversation_tasks "
        "where conversation_id = :c and parent_task_id is not null",
        c=e["conv"],
    )


ENTRADAS = {"fecha": "2026-11-03", "hora": "10:00", "nombre": "Ana Pérez"}


# ---------------------------------------------------------------- encargo
async def test_encargo_que_se_completa_sin_llm_con_resultado_tipado() -> None:
    e = await _escenario(clases=["public", "internal", "pii"])
    tools = {t.name: t for t in e["motor"].comandos()}
    assert "encargar" in tools and tools["encargar"].parameters["properties"]["procedimiento"]["enum"] == [DEMO.id]
    r = await _comando(e, "encargar", {"agente": "agenda", "procedimiento": DEMO.id, "entradas": ENTRADAS})
    assert r["ok"] is True, r
    assert r["result"] == ENTRADAS and "notice" in r
    ((pid, estado, dueno, padre, slots),) = await _subtareas(e)
    assert (pid, estado, dueno, padre) == (DEMO.id, "completed", e["inst"]["agenda"], e["foco"].fila.id)
    assert slots["nombre"]["value"] == "Ana Pérez"
    assert await _ledger(e["tid"], e["conv"]) == [("accepted", None), ("completed", None)]


async def test_sin_pii_en_el_permiso_el_dato_personal_se_niega() -> None:
    e = await _escenario(clases=["public", "internal"])
    r = await _comando(e, "encargar", {"agente": "agenda", "procedimiento": DEMO.id, "entradas": ENTRADAS})
    assert r["ok"] is False
    assert await _subtareas(e) == []
    assert await _ledger(e["tid"], e["conv"]) == [("denied", "data_class")]


async def test_encargo_incompleto_no_deja_nada() -> None:
    e = await _escenario(clases=["public", "internal", "pii"])
    r = await _comando(e, "encargar", {"agente": "agenda", "procedimiento": DEMO.id, "entradas": {"fecha": "2026-11-03", "nombre": "Ana Pérez"}})
    assert r["ok"] is False and "hora" in r["errors"][0]
    assert await _subtareas(e) == []
    assert await _ledger(e["tid"], e["conv"]) == [("accepted", None), ("failed", "error")]


async def test_un_dato_personal_que_el_cliente_no_dijo_se_rechaza() -> None:
    e = await _escenario(clases=["public", "internal", "pii"])
    r = await _comando(e, "encargar", {"agente": "agenda", "procedimiento": DEMO.id, "entradas": {**ENTRADAS, "nombre": "Luis Gómez"}})
    assert r["ok"] is False
    assert await _subtareas(e) == []
    assert [t for t, _r in await _ledger(e["tid"], e["conv"])] == ["accepted", "failed"]


# ---------------------------------------------------------------- consulta
async def test_consulta_con_la_identidad_del_destino_y_salida_validada() -> None:
    e = await _escenario(clases=["public", "internal"])
    r = await _comando(e, "consultar", {"agente": "agenda", "consulta": "agenda.tipos", "parametros": {"x": 1}})
    assert r["ok"] is True and r["result"] == {"estado": "activa"}, r
    ((quien, accion, args, clave),) = e["llamadas"]
    assert (quien, accion) == ("agenda", "list_appointment_types") and clave.startswith("query-")
    assert "x" not in args  # solo los parámetros del input schema de la acción
    assert await _ledger(e["tid"], e["conv"]) == [("accepted", None), ("completed", None)]


async def test_salida_que_no_valida_el_schema_falla() -> None:
    e = await _escenario(clases=["public", "internal"])
    e["respuesta"].clear()
    e["respuesta"]["estado"] = 3
    r = await _comando(e, "consultar", {"agente": "agenda", "consulta": "agenda.tipos"})
    assert r["ok"] is False and "result" not in r
    assert await _ledger(e["tid"], e["conv"]) == [("accepted", None), ("failed", "error")]


async def test_una_accion_que_gobierna_el_motor_no_se_consulta() -> None:
    e = await _escenario(clases=["public", "internal"], gobernadas=["list_appointment_types"])
    assert "consultar" not in {t.name for t in e["motor"].comandos()}
    r = await _comando(e, "consultar", {"agente": "agenda", "consulta": "agenda.tipos"})
    assert r["ok"] is False and e["llamadas"] == []
    assert await _ledger(e["tid"], e["conv"]) == []

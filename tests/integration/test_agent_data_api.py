"""HU-AOS-47 · Mi agente y su data: API por agente, procedencia y aislamiento (ADR-0140).

- **Vista por agente:** Ventas y Agenda en la misma conexión; cada uno ve sus conversaciones
  (con conexión y versión), tareas en curso y cerradas, clientes y registros, sin mezclar.
- **Procedencia:** el loop (`_execute_tool_safely`) y `procedencia.registrar` con la release del turno escriben instancia, versión,
  conexión y turno; sin release (legacy) no escribe; el primero que crea gana.
- **Permisos (ADR-0136):** colaborador sin `agents` → 403; sin `conversations` → 403 en
  conversaciones y clientes y tareas sin `conversation_id`; sin el pack → sus registros no salen.
- **Aislamiento:** el agente de otro negocio es 404 sin eco del id.
- **Legacy:** un negocio sin agentes ve al General (virtual) con lo no atribuido.
- `/overview` no lo captura `/{agent_id}` del router de `agents` (orden de los routers).

Sin LLM: los turnos se siembran en `usage_ledger` como los escribe el loop.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_agentes_versionados import PASSWORD
from test_backfill_agentes import _sql
from test_byok_produccion import _negocio

pytestmark = [
    pytest.mark.aislamiento,  # API por agente entre negocios y permisos (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_BASE = "/api/v1/admin/agents"


# ---------------------------------------------------------------- siembra
async def _conexion(tid: uuid.UUID) -> uuid.UUID:
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id, label) "
        "values (:id, :t, 'web', 'webchat', :ext, 'webchat', 'Chat web')",
        id=cid, t=tid, ext=uniq("wc"),
    )
    return cid


async def _agente(tid: uuid.UUID, key: str, kind: str = "specialist") -> uuid.UUID:
    aid = uuid.uuid4()
    await _sql(
        tid,
        "insert into agents (id, tenant_id, key, kind, name) values (:id, :t, :k, :kind, :n)",
        id=aid, t=tid, k=key, kind=kind, n=key.capitalize(),
    )
    return aid


async def _version(tid: uuid.UUID, aid: uuid.UUID, numero: int) -> uuid.UUID:
    vid = uuid.uuid4()
    await _sql(
        tid,
        "insert into agent_versions (id, tenant_id, agent_id, version, label, contract, content, content_hash) "
        "values (:id, :t, :a, :v, :l, 'agentspec/3', '{}'::jsonb, :h)",
        id=vid, t=tid, a=aid, v=numero, l=f"v{numero}", h=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    return vid


async def _instancia(tid: uuid.UUID, cid: uuid.UUID, aid: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    await _sql(
        tid,
        "insert into agent_instances (id, tenant_id, connection_id, agent_id, role) "
        "values (:id, :t, :c, :a, 'specialist')",
        id=iid, t=tid, c=cid, a=aid,
    )
    return iid


async def _conversacion(tid: uuid.UUID, cid: uuid.UUID | None, nombre: str) -> uuid.UUID:
    cliente, conv = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid,
        "insert into customers (id, tenant_id, channel, external_id, name) values (:id, :t, 'web', :ext, :n)",
        id=cliente, t=tid, ext=uniq("cli"), n=nombre,
    )
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id) "
        "values (:id, :t, :c, 'web', :conn)",
        id=conv, t=tid, c=cliente, conn=cid,
    )
    return conv


async def _turno(
    tid: uuid.UUID, conv: uuid.UUID, iid: uuid.UUID | None, cid: uuid.UUID | None, vid: uuid.UUID | None = None
) -> None:
    await _sql(
        tid,
        "insert into usage_ledger (id, tenant_id, conversation_id, model, input_tokens, output_tokens, cost_usd, "
        "connection_id, agent_instance_id, agent_version_id) values (:id, :t, :c, 'fake', 10, 5, 0.01, :conn, :i, :v)",
        id=uuid.uuid4(), t=tid, c=conv, conn=cid, i=iid, v=vid,
    )


async def _tarea(tid: uuid.UUID, conv: uuid.UUID, iid: uuid.UUID | None, *, cerrada: bool) -> None:
    await _sql(
        tid,
        "insert into conversation_tasks (tenant_id, conversation_id, procedure_id, procedure_version, status, stage, "
        "owner_instance_id, completed_at, stack_position) values (:t, :c, :p, '1.0.0', :s, 'inicio', :i, :fin, :pos)",
        t=tid, c=conv, p="agendar" if cerrada else "vender", s="completed" if cerrada else "active",
        i=iid, fin=datetime.now(UTC) if cerrada else None, pos=1 if cerrada else 0,
    )


async def _registrar(
    tid: uuid.UUID, conv: uuid.UUID, cid: uuid.UUID, iid: uuid.UUID, vid: uuid.UUID, tool: Any, resultado: dict
) -> None:
    from core.agents import procedencia
    from core.shared import release_vigente
    from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease
    from core.shared.tenant import tenant_scope

    foco = AgenteCongelado(
        instance_id=iid, agent_id=uuid.uuid4(), key="x", role="specialist", version_id=vid, version=1,
        contract="agentspec/3", content={},
    )
    async with tenant_scope(tid):
        token = release_vigente.abrir()
        try:
            release_vigente.fijar(
                ArtefactosDeRelease(
                    tenant_id=tid, connection_id=cid, release_id=uuid.uuid4(), lane="stable", idle_minutes=30, foco=foco
                )
            )
            await procedencia.registrar(tool, resultado, conv)
        finally:
            release_vigente.cerrar(token)


def _tool(nombre: str, plugin: str) -> Any:
    """La tool real del pack, como la registra el MCPHost (con su `plugin_id`)."""
    from pack_calendar.skills import CalendarSkill
    from pack_commerce.skills import LeadSkill, OrderSkill

    for skill in (LeadSkill(), OrderSkill(), CalendarSkill()):
        for tool in skill.get_tools():
            if tool.name == nombre:
                tool.plugin_id = plugin
                return tool
    raise AssertionError(f"la tool {nombre} no existe")


async def _colaborador(tid: uuid.UUID, denegadas: tuple[str, ...]) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion in denegadas:
            session.add(AdminPermission(admin_id=admin.id, tenant_id=tid, section=seccion, access="none"))
    return email


async def _escenario() -> dict[str, Any]:
    tid = await _negocio()
    cid = await _conexion(tid)
    ventas, agenda = await _agente(tid, "ventas"), await _agente(tid, "agenda")
    v_ventas, v_agenda = await _version(tid, ventas, 3), await _version(tid, agenda, 1)
    i_ventas, i_agenda = await _instancia(tid, cid, ventas), await _instancia(tid, cid, agenda)
    c1 = await _conversacion(tid, cid, "Ana Compradora")
    c2 = await _conversacion(tid, cid, "Beto Paciente")
    await _turno(tid, c1, i_ventas, cid, v_ventas)
    await _turno(tid, c1, i_ventas, cid, v_ventas)
    await _turno(tid, c2, i_agenda, cid, v_agenda)
    await _tarea(tid, c1, i_ventas, cerrada=False)
    await _tarea(tid, c2, i_agenda, cerrada=True)
    return {
        "tid": tid, "cid": cid, "ventas": ventas, "agenda": agenda, "i_ventas": i_ventas, "i_agenda": i_agenda,
        "v_ventas": v_ventas, "v_agenda": v_agenda, "c1": c1, "c2": c2,
    }


# ---------------------------------------------------------------- tests
async def test_vista_por_agente_sin_mezclar(app_client) -> None:
    e = await _escenario()
    tid = e["tid"]
    pedido = _tool("crear_pedido", "pack_commerce")
    cita = _tool("create_calendar_event", "pack_calendar")
    await _registrar(tid, e["c1"], e["cid"], e["i_ventas"], e["v_ventas"], pedido,
                     {"order_id": str(uuid.uuid4()), "status": "confirmed", "public_order_number": "P-0007"})
    await _registrar(tid, e["c2"], e["cid"], e["i_agenda"], e["v_agenda"], cita,
                     {"event_id": "evt-1", "status": "created"})
    h = await api_headers(app_client, tid)  # master

    r = await app_client.get(f"{_BASE}/overview", headers=h)
    assert r.status_code == 200, r.text
    por_key = {i["key"]: i for i in r.json()["items"]}
    assert por_key["ventas"]["turns"] == 2 and por_key["agenda"]["turns"] == 1
    assert por_key["ventas"]["published"] is True
    assert [c["label"] for c in por_key["ventas"]["connections"]] == ["Chat web"]
    assert por_key["general"]["virtual"] is True  # sin backfill: el General ve lo no atribuido

    r = await app_client.get(f"{_BASE}/{e['ventas']}/conversations", headers=h)
    assert r.status_code == 200, r.text
    (conv,) = r.json()["items"]
    assert conv["conversation_id"] == str(e["c1"]) and conv["customer_name"] == "Ana Compradora"
    assert (conv["turns"], conv["agent_version"], conv["connection_label"]) == (2, 3, "Chat web")

    r = await app_client.get(f"{_BASE}/{e['ventas']}/tasks", headers=h, params={"state": "open"})
    assert [t["procedure_id"] for t in r.json()["items"]] == ["vender"]
    r = await app_client.get(f"{_BASE}/{e['ventas']}/tasks", headers=h, params={"state": "closed"})
    assert r.json()["total"] == 0
    r = await app_client.get(f"{_BASE}/{e['agenda']}/tasks", headers=h, params={"state": "closed"})
    assert [t["procedure_id"] for t in r.json()["items"]] == ["agendar"]

    r = await app_client.get(f"{_BASE}/{e['ventas']}/records", headers=h)
    (reg,) = r.json()["items"]
    assert (reg["kind"], reg["label"], reg["pack_id"], reg["agent_version"]) == ("pedido", "P-0007", "pack_commerce", 3)
    r = await app_client.get(f"{_BASE}/{e['agenda']}/records", headers=h, params={"kind": "cita"})
    assert [x["record_ref"] for x in r.json()["items"]] == ["evt-1"]

    r = await app_client.get(f"{_BASE}/{e['agenda']}/customers", headers=h)
    assert [c["name"] for c in r.json()["items"]] == ["Beto Paciente"]

    r = await app_client.get(f"{_BASE}/{e['ventas']}/usage", headers=h, params={"connection_id": str(e["cid"])})
    assert r.status_code == 200, r.text
    uso = r.json()
    assert uso["total"]["turns"] == 2 and uso["by_connection"][0]["connection_label"] == "Chat web"
    assert uso["consistency"]["scope"] == "negocio"

    # Filtro de conexión y período: otra conexión o un período viejo no traen nada.
    otro = await app_client.get(
        f"{_BASE}/{e['ventas']}/conversations", headers=h, params={"connection_id": str(uuid.uuid4())}
    )
    assert otro.json()["total"] == 0
    viejo = datetime.now(UTC) - timedelta(days=200)
    r = await app_client.get(
        f"{_BASE}/{e['ventas']}/conversations", headers=h,
        params={"desde": viejo.isoformat(), "hasta": (viejo + timedelta(days=1)).isoformat()},
    )
    assert r.json()["total"] == 0
    r = await app_client.get(f"{_BASE}/{e['ventas']}/usage", headers=h, params={"desde": viejo.isoformat(), "hasta": (viejo - timedelta(days=1)).isoformat()})
    assert r.status_code == 400


async def test_procedencia_primero_gana_y_legacy_no_escribe() -> None:
    from core.agents import procedencia
    from core.shared.tenant import tenant_scope

    e = await _escenario()
    tid = e["tid"]
    lead = _tool("crear_lead", "pack_commerce")
    ref = str(uuid.uuid4())
    await _registrar(tid, e["c1"], e["cid"], e["i_ventas"], e["v_ventas"], lead, {"lead_id": ref, "status": "created"})
    # Otro agente lo «crea» después (no debería): gana el primero.
    await _registrar(tid, e["c2"], e["cid"], e["i_agenda"], e["v_agenda"], lead, {"lead_id": ref, "status": "created"})
    # El lead que ya existía no se atribuye.
    await _registrar(tid, e["c2"], e["cid"], e["i_agenda"], e["v_agenda"], lead,
                     {"lead_id": str(uuid.uuid4()), "status": "ya_existe"})
    # Sin release (legacy): nada.
    async with tenant_scope(tid):
        await procedencia.registrar(lead, {"lead_id": str(uuid.uuid4()), "status": "created"}, e["c1"])
    filas = await _sql(
        tid, "select agent_instance_id, connection_id, kind from agent_record_origins where tenant_id = :t", t=tid
    )
    assert [(f[0], f[1], f[2]) for f in filas] == [(e["i_ventas"], e["cid"], "lead")]
    # Append-only.
    with pytest.raises(Exception, match=r"(?i)permission denied|append|mutation"):
        await _sql(tid, "update agent_record_origins set label = 'x' where tenant_id = :t", t=tid)


async def test_permisos_de_colaborador(app_client) -> None:
    e = await _escenario()
    tid = e["tid"]
    pedido = _tool("crear_pedido", "pack_commerce")
    await _registrar(tid, e["c1"], e["cid"], e["i_ventas"], e["v_ventas"], pedido,
                     {"order_id": str(uuid.uuid4()), "status": "confirmed"})
    base = f"{_BASE}/{e['ventas']}"

    sin_agentes = await api_headers(app_client, tid, email=await _colaborador(tid, ("agents",)), password=PASSWORD)
    for ruta in ("/overview", f"/{e['ventas']}/usage", f"/{e['ventas']}/tasks"):
        assert (await app_client.get(f"{_BASE}{ruta}", headers=sin_agentes)).status_code == 403

    sin_conv = await api_headers(
        app_client, tid, email=await _colaborador(tid, ("conversations", "pack_commerce")), password=PASSWORD
    )
    assert (await app_client.get(f"{base}/conversations", headers=sin_conv)).status_code == 403
    assert (await app_client.get(f"{base}/customers", headers=sin_conv)).status_code == 403
    tareas = (await app_client.get(f"{base}/tasks", headers=sin_conv)).json()["items"]
    assert tareas and all(t["conversation_id"] is None for t in tareas)
    assert (await app_client.get(f"{base}/records", headers=sin_conv)).json()["total"] == 0  # sin el pack
    assert (await app_client.get(f"{base}/usage", headers=sin_conv)).status_code == 200

    # El costo en USD solo con `costs` (como la consola y el editor); turnos y tokens siempre.
    sin_costos = await api_headers(app_client, tid, email=await _colaborador(tid, ("costs",)), password=PASSWORD)
    uso = (await app_client.get(f"{base}/usage", headers=sin_costos)).json()
    assert uso["total"]["cost_usd"] is None and uso["total"]["turns"] == 2
    assert all(f["cost_usd"] is None for f in uso["by_connection"] + uso["by_day"])
    overview = (await app_client.get(f"{_BASE}/overview", headers=sin_costos)).json()
    assert all(i["cost_usd"] is None for i in overview["items"]) and overview["sin_atribuir"]["cost_usd"] is None
    assert next(i for i in overview["items"] if i["key"] == "ventas")["turns"] == 2
    convs = (await app_client.get(f"{base}/conversations", headers=sin_costos)).json()["items"]
    assert convs and all(c["cost_usd"] is None for c in convs)

    lector = await api_headers(app_client, tid, email=await _colaborador(tid, ()), password=PASSWORD)
    uso = (await app_client.get(f"{base}/usage", headers=lector)).json()
    assert uso["total"]["cost_usd"] > 0
    overview = (await app_client.get(f"{_BASE}/overview", headers=lector)).json()
    assert next(i for i in overview["items"] if i["key"] == "ventas")["cost_usd"] > 0
    assert (await app_client.get(f"{base}/records", headers=lector)).json()["total"] == 1
    assert (await app_client.get(f"{base}/conversations", headers=lector)).status_code == 200


async def test_aislamiento_entre_negocios(app_client) -> None:
    a = await _escenario()
    b = await _negocio()
    hb = await api_headers(app_client, b)
    for sufijo in ("conversations", "tasks", "records", "usage", "customers"):
        r = await app_client.get(f"{_BASE}/{a['ventas']}/{sufijo}", headers=hb)
        assert r.status_code == 404, (sufijo, r.text)
        assert str(a["ventas"]) not in r.text
    # Un ref que no es id ni `general` también es «no encontrado».
    assert (await app_client.get(f"{_BASE}/ventas/usage", headers=hb)).status_code == 404
    # Y el overview de B no ve nada de A, tampoco lo no atribuido.
    huerfana = await _conversacion(a["tid"], None, "Sin atribuir de A")
    await _turno(a["tid"], huerfana, None, None)
    overview = (await app_client.get(f"{_BASE}/overview", headers=hb)).json()
    assert all(i["key"] != "ventas" for i in overview["items"])
    assert overview["sin_atribuir"]["turns"] == 0
    for sufijo in ("conversations", "tasks", "customers"):
        r = await app_client.get(f"{_BASE}/general/{sufijo}", headers=hb)
        assert r.status_code == 200 and r.json()["total"] == 0, (sufijo, r.text)
    # La conexión de A como filtro desde B no trae nada.
    r = await app_client.get(f"{_BASE}/general/usage", headers=hb, params={"connection_id": str(a["cid"])})
    assert r.json()["total"]["turns"] == 0
    # RLS WITH CHECK: bajo el scope de B no se escribe procedencia de A.
    with pytest.raises(Exception, match=r"(?i)row-level security|violates"):
        await _sql(
            b,
            "insert into agent_record_origins (tenant_id, pack_id, kind, record_ref, tool) "
            "values (:t, 'pack_commerce', 'pedido', 'x', 'crear_pedido')",
            t=a["tid"],
        )


async def test_negocio_legacy_ve_al_general_con_lo_que_haya(app_client) -> None:
    tid = await _negocio()
    conv = await _conversacion(tid, None, "Cliente Legacy")
    await _turno(tid, conv, None, None)
    await _tarea(tid, conv, None, cerrada=False)
    h = await api_headers(app_client, tid)
    overview = (await app_client.get(f"{_BASE}/overview", headers=h)).json()
    assert overview["runtime"] == "legacy"
    (general,) = overview["items"]
    assert (general["ref"], general["virtual"], general["turns"]) == ("general", True, 1)
    r = await app_client.get(f"{_BASE}/general/conversations", headers=h)
    assert r.status_code == 200 and r.json()["items"][0]["customer_name"] == "Cliente Legacy"
    assert (await app_client.get(f"{_BASE}/general/tasks", headers=h)).json()["total"] == 1
    assert (await app_client.get(f"{_BASE}/general/records", headers=h)).json()["total"] == 0
    assert (await app_client.get(f"{_BASE}/general/usage", headers=h)).json()["incluye_sin_atribuir"] is True


async def test_el_loop_guarda_la_procedencia_al_ejecutar_la_tool() -> None:
    """Plomería del turno: `_execute_tool_safely` (el único punto donde la tool corre) deja la
    procedencia con el agente en foco de la release del turno."""
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.llm.types import LLMToolCall
    from core.mcp.host import mcp_host
    from core.mcp.types import BaseSkill, MCPTool, RegistroCreado
    from core.orchestrator.loop import _execute_tool_safely
    from core.shared import release_vigente
    from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease
    from core.shared.tenant import tenant_scope

    ref = f"R-{uuid.uuid4().hex[:8]}"

    class _Crea(BaseSkill):
        name = "skill_de_prueba_procedencia"

        async def _handler(self, _args: dict, _ctx: Any) -> dict:
            return {"reserva_id": ref, "status": "created"}

        def get_tools(self) -> list[MCPTool]:
            return [
                MCPTool(
                    name="tool_de_prueba_procedencia",
                    description="crea una reserva de prueba",
                    parameters={"type": "object", "properties": {}},
                    handler=self._handler,
                    crea_registro=RegistroCreado(
                        kind="reserva", id_key="reserva_id", es_nuevo=lambda r: r.get("status") == "created"
                    ),
                )
            ]

    e = await _escenario()
    tid = e["tid"]
    mcp_host.begin_activation("pack_commerce")
    mcp_host.register_skill(_Crea())
    mcp_host.end_activation()
    try:
        async with tenant_scope(tid):
            async with get_session() as session:
                conversation = await session.get(Conversation, e["c1"])
                customer = await session.get(Customer, conversation.customer_id) if conversation else None
            assert conversation is not None and customer is not None
            foco = AgenteCongelado(
                instance_id=e["i_ventas"], agent_id=e["ventas"], key="ventas", role="specialist",
                version_id=e["v_ventas"], version=3, contract="agentspec/3", content={},
            )
            token = release_vigente.abrir()
            try:
                release_vigente.fijar(
                    ArtefactosDeRelease(
                        tenant_id=tid, connection_id=e["cid"], release_id=uuid.uuid4(), lane="stable",
                        idle_minutes=30, foco=foco,
                    )
                )
                llamada = LLMToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name="tool_de_prueba_procedencia", arguments={})
                resultado = await _execute_tool_safely(conversation, customer, llamada)
            finally:
                release_vigente.cerrar(token)
        assert resultado.get("reserva_id") == ref
        filas = await _sql(
            tid,
            "select pack_id, kind, agent_instance_id, agent_version_id, connection_id, conversation_id, tool "
            "from agent_record_origins where tenant_id = :t and record_ref = :r",
            t=tid, r=ref,
        )
        assert [tuple(f) for f in filas] == [
            ("pack_commerce", "reserva", e["i_ventas"], e["v_ventas"], e["cid"], e["c1"], "tool_de_prueba_procedencia")
        ]
    finally:
        mcp_host._skills.pop("skill_de_prueba_procedencia", None)
        mcp_host._tools.pop("tool_de_prueba_procedencia", None)

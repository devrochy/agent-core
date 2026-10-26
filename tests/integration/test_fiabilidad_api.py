"""HU-AOS-29 · tablero de fiabilidad: métricas, verificador, completitud, consistencia y permisos.

- **Operativas:** cuentan los turnos `routed` y `escalated` (no los bloqueos); el escalamiento suma
  el `outcome` y el `status`; los turnos sin `turn_outcomes` salen como `without_outcome`; el dinero
  sale de `usage_ledger` sin `eval`.
- **Filtros:** versión y carril se aplican a todo; una versión o carril sin turnos da cero.
- **Verificador:** rechazos por tipo menos los falsos positivos; la marca es idempotente y solo
  vale para un rechazo real del turno.
- **Completitud:** por `completed_at` en la ventana; bajo 10 tareas la tasa es `null`.
- **Consistencia vigente:** recorre la cadena `base_release_id`; lo tocado sin medir sale sin datos.
- **Permisos y aislamiento:** sin `agents` 403; sin `costs` el dinero en null; sin `conversations`
  sin `conversation_id`; los datos de otro negocio no aparecen y su turno es 404.

Sin LLM: los turnos se siembran en el ledger como los escribe el kernel.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_gate_veredictos import _candidata, _modules, _negocio, _sql

pytestmark = [
    pytest.mark.aislamiento,  # métricas por negocio, permisos y marca de falsos positivos (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"
_BASE = "/api/v1/admin"


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte


# ---------------------------------------------------------------- siembra
async def _instancia(tid: uuid.UUID, cid: uuid.UUID, key: str) -> uuid.UUID:
    aid, iid = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid, "insert into agents (id, tenant_id, key, kind, name) values (:id, :t, :k, 'specialist', :n)",
        id=aid, t=tid, k=key, n=key.capitalize(),
    )
    await _sql(
        tid,
        "insert into agent_instances (id, tenant_id, connection_id, agent_id, role) values (:id, :t, :c, :a, 'specialist')",
        id=iid, t=tid, c=cid, a=aid,
    )
    return iid


async def _conversacion(tid: uuid.UUID, cid: uuid.UUID) -> uuid.UUID:
    cliente, conv = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid, "insert into customers (id, tenant_id, channel, external_id, name) values (:id, :t, 'web', :e, 'Cliente')",
        id=cliente, t=tid, e=uniq("cli"),
    )
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id) values (:id, :t, :c, 'web', :conn)",
        id=conv, t=tid, c=cliente, conn=cid,
    )
    return conv


async def _turno(
    tid: uuid.UUID,
    cid: uuid.UUID,
    rid: uuid.UUID,
    conv: uuid.UUID,
    iid: uuid.UUID,
    *,
    outcome: str = "routed",
    status: str | None = "replied",
    verifier: list[dict[str, Any]] | None = None,
    costo: float = 0.01,
) -> uuid.UUID:
    turno = uuid.uuid4()
    await _sql(
        tid,
        "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_id, release_lane, outcome, "
        "blocked_by, active_instance_id, latency_ms) values (:id, :t, :c, :conv, :r, 'stable', :o, :b, :i, 5)",
        id=turno, t=tid, c=cid, conv=conv, r=rid, o=outcome, b="kill_switch" if outcome == "blocked" else None, i=iid,
    )
    if status is not None:
        await _sql(
            tid,
            "insert into turn_outcomes (turn_id, tenant_id, status, verifier, final_instance_id, latency_ms) "
            "values (:id, :t, :s, cast(:v as jsonb), :i, 1200)",
            id=turno, t=tid, s=status, v=json.dumps(verifier or []), i=iid,
        )
    if outcome != "blocked":
        await _sql(
            tid,
            "insert into usage_ledger (id, tenant_id, conversation_id, model, input_tokens, output_tokens, cost_usd, kind, "
            "turn_id, connection_id, agent_instance_id, release_id) "
            "values (:id, :t, :conv, 'fake', 10, 5, :costo, 'turn', :turno, :c, :i, :r)",
            id=uuid.uuid4(), t=tid, conv=conv, costo=costo, turno=turno, c=cid, i=iid, r=rid,
        )
    return turno


async def _tarea(
    tid: uuid.UUID, conv: uuid.UUID, procedimiento: str, status: str, turno: uuid.UUID, *, hace_dias: int = 1
) -> None:
    await _sql(
        tid,
        "insert into conversation_tasks (tenant_id, conversation_id, procedure_id, procedure_version, status, stage, "
        "completed_at, stack_position, started_turn_id, status_reason) "
        "values (:t, :c, :p, '1.0.0', :s, 'fin', :fin, 1, :turno, :motivo)",
        t=tid, c=conv, p=procedimiento, s=status, fin=datetime.now(UTC) - timedelta(days=hace_dias), turno=turno,
        motivo=None if status == "completed" else "pidio_humano",
    )


async def _colaborador(tid: uuid.UUID, permisos: dict[str, str]) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion, acceso in permisos.items():
            session.add(AdminPermission(admin_id=admin.id, tenant_id=tid, section=seccion, access=acceso))
    return email


async def _escenario() -> dict[str, Any]:
    tid, cid, rid = await _negocio()
    ventas, agenda = await _instancia(tid, cid, "ventas"), await _instancia(tid, cid, "agenda")
    a, b, c = [await _conversacion(tid, cid) for _ in range(3)]
    turnos_a = [await _turno(tid, cid, rid, a, ventas) for _ in range(9)]
    con_rechazo = await _turno(
        tid, cid, rid, a, ventas,
        verifier=[{"kind": "precio", "verdict": "fail", "count": 1}, {"kind": "tono", "verdict": "pass", "count": 1}],
    )
    await _turno(tid, cid, rid, a, ventas, outcome="blocked", status=None)  # no cuenta
    for i in range(10):
        await _turno(
            tid, cid, rid, b, agenda, status="escalated" if i < 2 else "replied",
            verifier=[{"kind": "precio" if i == 5 else "datos", "verdict": "fail"}] if i in (5, 6) else None,
        )
    for _ in range(4):
        await _turno(tid, cid, rid, c, agenda, outcome="escalated", status="escalated")
    await _turno(tid, cid, rid, c, agenda, status=None)  # sin resultado (ledger best-effort)

    # Gasto que NO es actividad del negocio (eval) y una llamada A2A con su `agent_run`.
    await _sql(
        tid,
        "insert into usage_ledger (id, tenant_id, model, input_tokens, output_tokens, cost_usd, kind, connection_id, release_id) "
        "values (:id, :t, 'fake', 1, 1, 5.0, 'eval', :c, :r)",
        id=uuid.uuid4(), t=tid, c=cid, r=rid,
    )
    a2a_conv, call = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid,
        "insert into a2a_conversations (id, tenant_id, connection_id, trigger, root_mode, initiator_instance_id, "
        "max_depth, budget_usd, budget_tokens, deadline_at) values (:id, :t, :c, 'event', 'query', :i, 2, 1, 1000, now())",
        id=a2a_conv, t=tid, c=cid, i=ventas,
    )
    await _sql(
        tid,
        "insert into a2a_events (tenant_id, a2a_conversation_id, seq, call_id, type, mode, depth, from_instance_id, "
        "to_instance_id, intent) values (:t, :ac, 1, :call, 'requested', 'query', 1, :de, :a, 'consultar_agenda')",
        t=tid, ac=a2a_conv, call=call, de=ventas, a=agenda,
    )
    await _sql(
        tid,
        "insert into usage_ledger (id, tenant_id, conversation_id, model, input_tokens, output_tokens, cost_usd, kind, "
        "turn_id, connection_id, agent_instance_id, release_id, a2a_call_id) "
        "values (:id, :t, :conv, 'fake', 3, 3, 0.02, 'agent_run', :turno, :c, :i, :r, :call)",
        id=uuid.uuid4(), t=tid, conv=a, turno=turnos_a[0], c=cid, i=agenda, r=rid, call=call,
    )

    for i in range(12):
        await _tarea(tid, a, "p.vender", "completed" if i < 9 else "escalated", turnos_a[0])
    await _tarea(tid, a, "p.vender", "completed", turnos_a[0], hace_dias=60)  # fuera de la ventana
    await _tarea(tid, b, "p.otra", "completed", turnos_a[0])
    return {"tid": tid, "cid": cid, "rid": rid, "ventas": ventas, "agenda": agenda, "a": a, "con_rechazo": con_rechazo}


# ---------------------------------------------------------------- tests
async def test_metricas_operativas_verificador_y_completitud(app_client) -> None:
    e = await _escenario()
    h = await api_headers(app_client, e["tid"])  # master
    r = await app_client.get(f"{_BASE}/reliability", headers=h, params={"connection_id": str(e["cid"])})
    assert r.status_code == 200, r.text
    datos = r.json()
    op = datos["operational"]
    assert op["turns"] == 25 and op["conversations"] == 3
    assert op["escalated"] == 6 and op["escalated_conversations"] == 2
    assert op["without_outcome"] == 1
    assert op["escalation_rate"] == pytest.approx(6 / 25)
    assert op["p95_ms"] == 1200
    assert op["cost_usd"] == pytest.approx(25 * 0.01 + 0.02)  # sin el `eval`
    ventas = next(a for a in op["by_agent"] if a["name"] == "Ventas")
    assert ventas["turns"] == 10 and ventas["escalation_rate"] is None  # bajo la muestra mínima

    verificador = {v["kind"]: v for v in datos["verifier"]}
    assert verificador["precio"]["rejections"] == 2 and verificador["precio"]["false_positives"] == 0
    assert verificador["datos"]["rejections"] == 1 and "tono" not in verificador

    completitud = {c["procedure_id"]: c for c in datos["completion"]}
    assert completitud["p.vender"]["finished"] == 12 and completitud["p.vender"]["completed"] == 9
    assert completitud["p.vender"]["rate"] == pytest.approx(0.75)
    assert completitud["p.otra"]["rate"] is None  # bajo 10 tareas

    # Versión o carril sin turnos: cero en todo.
    for filtro in ({"lane": "canary"}, {"release_id": str(uuid.uuid4())}):
        vacio = (await app_client.get(f"{_BASE}/reliability", headers=h, params=filtro)).json()
        assert vacio["operational"]["turns"] == 0 and vacio["verifier"] == [] and vacio["completion"] == []
    assert (await app_client.get(f"{_BASE}/reliability", headers=h, params={"lane": "x"})).status_code == 422

    # Gasto agrupado: por agente, por día y por operación A2A (quién pide, a quién y modo).
    uso = (await app_client.get(f"{_BASE}/usage", headers=h, params={"group_by": "a2a"})).json()
    assert [(i["name"], i["mode"], i["cost_usd"]) for i in uso["items"]] == [("Ventas → Agenda", "query", 0.02)]
    por_dia = (await app_client.get(f"{_BASE}/usage", headers=h, params={"group_by": "day"})).json()
    assert sum(i["cost_usd"] for i in por_dia["items"]) == pytest.approx(0.27)
    por_conexion = (await app_client.get(f"{_BASE}/usage", headers=h, params={"group_by": "connection"})).json()
    assert [i["key"] for i in por_conexion["items"]] == [str(e["cid"])]


async def test_falso_positivo_idempotente_y_solo_de_un_rechazo_real(app_client) -> None:
    e = await _escenario()
    h = await api_headers(app_client, e["tid"])
    url = f"{_BASE}/reliability/verifier/{e['con_rechazo']}/false-positive"
    for _ in range(2):
        r = await app_client.post(url, headers=h, json={"kind": "precio", "note": "El precio era correcto"})
        assert r.status_code == 200, r.text
    filas = await _sql(
        e["tid"], "select count(*) from turn_feedback where turn_id = :t and source = 'owner'", t=e["con_rechazo"]
    )
    assert filas[0][0] == 1
    # «tono» pasó: no es un rechazo. Un turno inexistente tampoco.
    assert (await app_client.post(url, headers=h, json={"kind": "tono"})).status_code == 404
    otro = f"{_BASE}/reliability/verifier/{uuid.uuid4()}/false-positive"
    assert (await app_client.post(otro, headers=h, json={"kind": "precio"})).status_code == 404
    assert (await app_client.post(url, headers=h, json={"kind": "Precio!"})).status_code == 422

    datos = (await app_client.get(f"{_BASE}/reliability", headers=h)).json()
    precio = next(v for v in datos["verifier"] if v["kind"] == "precio")
    assert precio["rejections"] == 2 and precio["false_positives"] == 1
    lista = (await app_client.get(f"{_BASE}/reliability/verifier", headers=h, params={"kind": "precio"})).json()
    assert len(lista["items"]) == 2
    marcado = next(i for i in lista["items"] if i["turn_id"] == str(e["con_rechazo"]))
    assert marcado["false_positive"] and marcado["conversation_id"] == str(e["a"])


async def test_permisos_de_colaborador(app_client) -> None:
    e = await _escenario()
    tid = e["tid"]

    sin_agentes = await api_headers(app_client, tid, email=await _colaborador(tid, {"agents": "none"}), password=PASSWORD)
    for ruta in ("/reliability", "/reliability/verifier", "/usage", "/evals/runs"):
        assert (await app_client.get(f"{_BASE}{ruta}", headers=sin_agentes)).status_code == 403

    restringido = await api_headers(
        app_client, tid,
        email=await _colaborador(tid, {"agents": "read", "costs": "none", "conversations": "none"}),
        password=PASSWORD,
    )
    datos = (await app_client.get(f"{_BASE}/reliability", headers=restringido)).json()
    assert datos["operational"]["turns"] == 25
    assert datos["operational"]["cost_usd"] is None and datos["operational"]["cost_per_conversation"] is None
    assert all(a["cost_usd"] is None for a in datos["operational"]["by_agent"])
    uso = (await app_client.get(f"{_BASE}/usage", headers=restringido)).json()
    assert uso["items"] and all(i["cost_usd"] is None for i in uso["items"])
    lista = (await app_client.get(f"{_BASE}/reliability/verifier", headers=restringido)).json()
    assert lista["items"] and all(i["conversation_id"] is None for i in lista["items"])
    # Solo lectura en `agents`: no marca.
    url = f"{_BASE}/reliability/verifier/{e['con_rechazo']}/false-positive"
    assert (await app_client.post(url, headers=restringido, json={"kind": "precio"})).status_code == 403


async def test_aislamiento_entre_negocios(app_client) -> None:
    e = await _escenario()
    otro, _cid, _rid = await _negocio()
    hb = await api_headers(app_client, otro)
    datos = (await app_client.get(f"{_BASE}/reliability", headers=hb, params={"connection_id": str(e["cid"])})).json()
    assert datos["operational"]["turns"] == 0 and datos["verifier"] == [] and datos["consistency"] == []
    uso = (await app_client.get(f"{_BASE}/usage", headers=hb, params={"connection_id": str(e["cid"])})).json()
    assert uso["items"] == []
    assert (await app_client.get(f"{_BASE}/reliability/verifier", headers=hb)).json()["items"] == []
    url = f"{_BASE}/reliability/verifier/{e['con_rechazo']}/false-positive"
    r = await app_client.post(url, headers=hb, json={"kind": "precio"})
    assert r.status_code == 404 and str(e["con_rechazo"]) not in r.text


async def _veredicto(
    tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, base: uuid.UUID, *, mide: dict[str, float],
    tocados: list[str], sin_medir: list[str],
) -> None:
    from core.releases import veredicto as v
    from core.shared.tenant import tenant_scope

    mediciones = [
        v.Medicion(pid, tuple(v.Escenario(f"{i:02d}", 4, 1.0, 1.0 if i < round(valor * 4) else 0.0, True) for i in range(4)), "sha")
        for pid, valor in mide.items()
    ]
    async with tenant_scope(tid):
        await v.registrar(
            tid, cid, rid, base_release_id=base, tocados=dict.fromkeys(tocados, "test"), mediciones=mediciones,
            veredicto=v.calcular(mediciones), costo_usd=0.1, modelo="test-model", sin_medir=sin_medir,
        )


async def test_consistencia_vigente_recorre_la_cadena(app_client) -> None:
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.releases.veredicto import consistencia_vigente
    from core.shared.tenant import tenant_scope

    tid, cid, r1 = await _negocio()
    h = await api_headers(app_client, tid)
    sin_version = (await app_client.get(f"{_BASE}/reliability", headers=h)).json()["consistency"]
    assert [c["no_data"] for c in sin_version] == [True]  # la conexión aparece, sin mediciones

    async with tenant_scope(tid):
        async with get_session() as session:
            procs = sorted((await cargar_release(session, tid, r1)).procedimientos())
    assert len(procs) >= 2, procs
    p, q = procs[0], procs[1]

    async def activar(rid: uuid.UUID) -> None:
        await _sql(tid, "update channel_connections set active_release_id = :r where id = :c", r=rid, c=cid)

    async def vigente(rid: uuid.UUID) -> dict[str, dict[str, Any]]:
        async with tenant_scope(tid):
            async with get_session() as session:
                return {x["procedure_id"]: x for x in await consistencia_vigente(session, tid, rid)}

    # v2 mide P (bajo el umbral) y Q; v3 no toca nada: hereda de v2.
    r2 = await _candidata(tid, cid, "Asistente dos")
    await _veredicto(tid, cid, r2, r1, mide={p: 0.5, q: 1.0}, tocados=[p, q], sin_medir=[])
    r3 = await _candidata(tid, cid, "Asistente tres")
    await _veredicto(tid, cid, r3, r2, mide={}, tocados=[], sin_medir=[])
    v3 = await vigente(r3)
    assert v3[p]["pass_k"] == 0.5 and v3[p]["ok"] is False and v3[p]["medido_en_release_id"] == r2
    assert v3[q]["pass_k"] == 1.0

    # v4 toca Q sin medirlo: Q sale sin datos; P sigue heredando.
    r4 = await _candidata(tid, cid, "Asistente cuatro")
    await _veredicto(tid, cid, r4, r3, mide={}, tocados=[q], sin_medir=[q])
    v4 = await vigente(r4)
    assert v4[q]["pass_k"] is None and v4[p]["pass_k"] == 0.5

    await activar(r4)
    conexion = (await app_client.get(f"{_BASE}/reliability", headers=h)).json()["consistency"][0]
    assert conexion["release_id"] == str(r4) and conexion["below_threshold"] == [p] and not conexion["no_data"]

    corridas = (await app_client.get(f"{_BASE}/evals/runs", headers=h, params={"connection_id": str(cid)})).json()
    assert len(corridas["items"]) == 3 and all("cost_usd" not in c for c in corridas["items"])
    assert corridas["items"][-1]["pass_k_average"] == pytest.approx(0.75)  # v2: (2 + 4) escenarios estables de 8

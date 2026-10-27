"""Resumen por agente: `GET /admin/dashboard/agents` (ADR-0171).

- **Contenido:** KPIs del negocio y de cada agente (7 d contra los 7 d previos, con delta) y, bajo
  el agente, los módulos que gestiona; cada KPI con un destino semántico. Lo no atribuido es del
  General; los módulos que ningún agente cubre cuelgan del General.
- **Permisos (ADR-0136):** un KPI sin la sección que lo respalda se omite, no sale en cero.
- **Aislamiento:** un negocio no ve la actividad de otro.
- **Fallo aislado:** un módulo que lanza no tumba el resto.
- **Proveedores reales:** cada pack calcula sus KPIs sobre un negocio sin datos sin romperse.

Sin LLM: los turnos se siembran en `usage_ledger` como los escribe el loop.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import api_headers, banderas_como_dueno
from test_agent_data_api import (
    _agente,
    _colaborador,
    _conexion,
    _conversacion,
    _instancia,
    _tarea,
)
from test_agentes_versionados import PASSWORD
from test_backfill_agentes import _sql
from test_byok_produccion import _negocio

pytestmark = [
    pytest.mark.aislamiento,  # KPIs por agente entre negocios y permisos (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_URL = "/api/v1/admin/dashboard/agents"
_PACKS = {"pack_commerce", "pack_calendar", "pack_cobranza", "pack_subscriptions"}


@pytest.fixture(autouse=True)
def _packs_habilitados(monkeypatch) -> None:
    """El spec de un negocio nuevo no trae estos packs; los módulos dependen de que estén activos."""

    async def _todos() -> set[str]:
        return set(_PACKS)

    monkeypatch.setattr("core.metricas.resumen_agentes._habilitados", _todos)


async def _turno_hace(tid, conv, iid, cid, dias: float, costo: float = 0.01) -> None:
    await _sql(
        tid,
        "insert into usage_ledger (id, tenant_id, conversation_id, model, input_tokens, output_tokens, "
        "cost_usd, connection_id, agent_instance_id, created_at) "
        "values (:id, :t, :c, 'fake', 10, 5, :costo, :conn, :i, :cuando)",
        id=uuid.uuid4(), t=tid, c=conv, conn=cid, i=iid, costo=costo,
        cuando=datetime.now(UTC) - timedelta(days=dias),
    )


async def _pedido(tid, conv, cliente, *, estado: str, pago: str, total: float = 100.0) -> None:
    seq = abs(hash(uuid.uuid4())) % 1_000_000
    await _sql(
        tid,
        "insert into pack_commerce_orders (id, tenant_id, conversation_id, customer_id, idempotency_key, "
        "order_seq, public_order_number, status, payment_status, total) "
        "values (:id, :t, :c, :cli, :k, :s, :n, :st, :p, :tot)",
        id=uuid.uuid4(), t=tid, c=conv, cli=cliente, k=uuid.uuid4().hex, s=seq, n=f"P-{seq}",
        st=estado, p=pago, tot=total,
    )


async def _cliente_de(tid, conv) -> uuid.UUID:
    return (await _sql(tid, "select customer_id from conversations where id = :c", c=conv))[0][0]


async def _escenario() -> dict:
    tid = await _negocio()
    cid = await _conexion(tid)
    ventas, agenda = await _agente(tid, "ventas"), await _agente(tid, "agenda")
    i_ventas, i_agenda = await _instancia(tid, cid, ventas), await _instancia(tid, cid, agenda)
    c1 = await _conversacion(tid, cid, "Ana Compradora")
    c2 = await _conversacion(tid, cid, "Beto Paciente")
    # Ventas: 3 turnos en los últimos 7 d y 1 en los 7 previos. Agenda: 1 y 0.
    for dias in (1, 2, 3, 9):
        await _turno_hace(tid, c1, i_ventas, cid, dias)
    await _turno_hace(tid, c2, i_agenda, cid, 1)
    await _turno_hace(tid, c2, None, cid, 1)  # no atribuido: del General
    await _tarea(tid, c1, i_ventas, cerrada=False)
    await _tarea(tid, c2, i_agenda, cerrada=True)
    cli = await _cliente_de(tid, c1)
    await _pedido(tid, c1, cli, estado="abierto", pago="pagado", total=250.0)
    await _pedido(tid, c1, cli, estado="entregado", pago="pagado", total=50.0)
    await _pedido(tid, c1, cli, estado="cancelado", pago="pendiente", total=999.0)  # no cuenta
    await _sql(
        tid,
        "insert into pack_calendar_appointments (id, tenant_id, start_at, end_at, status) "
        "values (:id, :t, :s, :e, 'scheduled')",
        id=uuid.uuid4(), t=tid, s=datetime.now(UTC) + timedelta(days=2),
        e=datetime.now(UTC) + timedelta(days=2, hours=1),
    )
    return {"tid": tid, "ventas": ventas, "agenda": agenda}


def _kpis(bloque: dict) -> dict:
    return {k["id"]: k for k in bloque["kpis"]}


def _agente_por_key(datos: dict, key: str) -> dict:
    return next(a for a in datos["agentes"] if a["key"] == key)


async def test_kpis_del_negocio_de_cada_agente_y_de_su_modulo(app_client) -> None:
    e = await _escenario()
    h = await api_headers(app_client, e["tid"])
    r = await app_client.get(_URL, headers=h)
    assert r.status_code == 200, r.text
    datos = r.json()
    assert datos["runtime"] == "legacy"

    general = _kpis(datos["general"])
    assert general["conversaciones"]["destino"] == {"ruta": "conversaciones", "params": {}}
    assert general["escalaciones_pendientes"]["valor"] == 0
    assert general["escalaciones_pendientes"]["polaridad"] == "up-bad"
    assert general["costo_llm"]["unidad"] == "usd"

    ventas = _agente_por_key(datos, "ventas")
    assert ventas["ref"] == str(e["ventas"])
    k = _kpis(ventas)
    assert (k["turnos"]["valor"], k["turnos"]["previo"], k["turnos"]["delta_pct"]) == (3, 1, 200)
    assert k["turnos"]["destino"] == {
        "ruta": "agente", "params": {"ref": str(e["ventas"]), "pestana": "conversaciones"},
    }
    assert k["tareas_abiertas"]["valor"] == 1 and k["tareas_abiertas"]["previo"] is None
    assert k["costo"]["valor"] == pytest.approx(0.03)
    assert "escalados" not in k  # legacy: no hay turn_outcomes que atribuir

    # El módulo es el del pack de la plantilla, completo: cuenta 2 pedidos (el cancelado no).
    (modulo,) = ventas["modulos"]
    assert (modulo["id"], modulo["nombre"], modulo["error"]) == ("pack_commerce", "Ventas", None)
    m = _kpis(modulo)
    assert m["pedidos"]["valor"] == 2
    assert m["ventas_pagadas"]["valor"] == 300 and m["ventas_pagadas"]["unidad"] == "money"
    assert m["por_despachar"]["valor"] == 1
    assert m["pedidos"]["destino"]["ruta"] == "comercio.pedidos"

    (agenda,) = _agente_por_key(datos, "agenda")["modulos"]
    assert agenda["id"] == "pack_calendar"
    assert _kpis(agenda)["citas_proximas"]["valor"] == 1

    # El General (sin fila: negocio legacy) absorbe lo no atribuido y los módulos sin agente.
    gen = _agente_por_key(datos, "general")
    assert gen["ref"] == "general" and _kpis(gen)["turnos"]["valor"] == 1
    assert {m["id"] for m in gen["modulos"]} == {"pack_cobranza", "pack_subscriptions"}


async def test_permisos_omiten_el_kpi_en_vez_de_mostrar_cero(app_client) -> None:
    e = await _escenario()
    tid = e["tid"]

    sin_costos = await api_headers(app_client, tid, email=await _colaborador(tid, ("costs",)), password=PASSWORD)
    datos = (await app_client.get(_URL, headers=sin_costos)).json()
    assert "costo_llm" not in _kpis(datos["general"])
    assert all("costo" not in _kpis(a) for a in datos["agentes"])
    assert _kpis(_agente_por_key(datos, "ventas"))["turnos"]["valor"] == 3  # lo demás sigue

    sin_agentes = await api_headers(app_client, tid, email=await _colaborador(tid, ("agents",)), password=PASSWORD)
    datos = (await app_client.get(_URL, headers=sin_agentes)).json()
    assert datos["agentes"] == []
    assert {"tasa_escalamiento", "consistencia"}.isdisjoint(_kpis(datos["general"]))
    assert "conversaciones" in _kpis(datos["general"])

    sin_ventas = await api_headers(app_client, tid, email=await _colaborador(tid, ("pack_commerce",)), password=PASSWORD)
    datos = (await app_client.get(_URL, headers=sin_ventas)).json()
    assert _agente_por_key(datos, "ventas")["modulos"] == []
    assert [m["id"] for m in _agente_por_key(datos, "agenda")["modulos"]] == ["pack_calendar"]

    sin_dashboard = await api_headers(app_client, tid, email=await _colaborador(tid, ("dashboard",)), password=PASSWORD)
    assert (await app_client.get(_URL, headers=sin_dashboard)).status_code == 403


async def test_un_negocio_no_ve_la_actividad_de_otro(app_client) -> None:
    await _escenario()
    otro = await _negocio()
    datos = (await app_client.get(_URL, headers=await api_headers(app_client, otro))).json()
    (general,) = datos["agentes"]  # solo el General virtual, sin los agentes de A
    assert general["key"] == "general"
    assert _kpis(general)["turnos"]["valor"] == 0
    assert _kpis(datos["general"])["conversaciones"]["valor"] == 0
    assert _kpis(general)["costo"]["valor"] == 0


async def test_un_modulo_que_falla_no_tumba_el_resumen(app_client, monkeypatch) -> None:
    from core.admin.resumen_registry import ResumenProvider, resumen_registry

    async def _roto(_tid, _p):
        raise RuntimeError("tabla del pack caída")

    monkeypatch.setitem(
        resumen_registry._providers,
        "pack_commerce",
        ResumenProvider("pack_commerce", "ventas", "Ventas", "pack_commerce", _roto),
    )
    e = await _escenario()
    datos = (await app_client.get(_URL, headers=await api_headers(app_client, e["tid"]))).json()
    ventas = _agente_por_key(datos, "ventas")
    (modulo,) = ventas["modulos"]
    assert modulo["kpis"] == [] and modulo["error"]
    assert _kpis(ventas)["turnos"]["valor"] == 3
    assert _kpis(_agente_por_key(datos, "agenda")["modulos"][0])["citas_proximas"]["valor"] == 1


async def test_runtime_v3_cuenta_los_escalados_por_agente(app_client) -> None:
    e = await _escenario()
    tid = e["tid"]
    await banderas_como_dueno(tid, aos_runtime="v3")
    cid = (await _sql(tid, "select connection_id from agent_instances where agent_id = :a", a=e["ventas"]))[0][0]
    iid = (await _sql(tid, "select id from agent_instances where agent_id = :a", a=e["ventas"]))[0][0]
    conv = (await _sql(tid, "select id from conversations where tenant_id = :t limit 1", t=tid))[0][0]
    for resultado in ("escalated", "replied"):
        turno = uuid.uuid4()
        await _sql(
            tid,
            "insert into core_decisions (id, tenant_id, conversation_id, outcome, latency_ms, connection_id, "
            "active_instance_id) values (:id, :t, :c, 'routed', 10, :conn, :i)",
            id=turno, t=tid, c=conv, conn=cid, i=iid,
        )
        await _sql(
            tid,
            "insert into turn_outcomes (turn_id, tenant_id, status, latency_ms, final_instance_id) "
            "values (:id, :t, :s, 10, :i)",
            id=turno, t=tid, s=resultado, i=iid,
        )
    datos = (await app_client.get(_URL, headers=await api_headers(app_client, tid))).json()
    assert datos["runtime"] == "v3"
    esc = _kpis(_agente_por_key(datos, "ventas"))["escalados"]
    assert (esc["valor"], esc["polaridad"]) == (1, "up-bad")
    assert _kpis(_agente_por_key(datos, "agenda"))["escalados"]["valor"] == 0


async def test_proveedores_reales_responden_sin_datos() -> None:
    from core.admin.resumen_registry import resumen_registry
    from core.metricas.resumen_agentes import periodo
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    p = periodo()
    ids = {}
    async with tenant_scope(tid):
        for proveedor in resumen_registry.providers_for(None):
            kpis = await proveedor.kpis(tid, p)
            assert kpis and all(k.id and k.label for k in kpis), proveedor.plugin_id
            ids[proveedor.agent_key] = proveedor.plugin_id
    assert ids == {
        "ventas": "pack_commerce",
        "agenda": "pack_calendar",
        "cobranza": "pack_cobranza",
        "suscripciones": "pack_subscriptions",
    }

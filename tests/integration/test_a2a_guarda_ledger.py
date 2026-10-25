"""HU-AOS-18 · grafo de permisos A2A, congelado en la release, guarda con ledger y API.

Sin LLM. La guarda se llama directo (las operaciones que la usan llegan con su HU):

- publicar el grafo es idempotente por hash, versiona y no acepta agentes de otro negocio;
- la release congela el grafo (y su `graph_version_id`) solo cuando existe; un enlace que pide más
  que el Coordinador hace fallar el build;
- un ciclo A→B→A deja `loop_detected` en el ledger, la conversación A2A en `loop_blocked` y avisa
  por el bus; `liquidar` escribe el gasto como `agent_run` con su `a2a_call_id`;
- sin cabecera del turno no se escribe nada;
- las FK compuestas rechazan referencias a otro negocio;
- la API: 422 con broadcast, 403 en legacy para quien no es master, y las llamadas agrupadas.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from typing import Any

import pytest
from _api_helpers import api_headers, banderas_como_dueno, uniq
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte


async def _negocio(*, v3: bool = True) -> dict[str, Any]:
    """Negocio con una conexión, su release 1 y dos especialistas (ventas, agenda)."""
    from core.agents import instancias, versiones
    from core.alta_negocio import onboard_tenant
    from core.kernel.procedures import catalogo_de_acciones
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    slug = uniq("a2a")
    tid, _ = await onboard_tenant(name=f"A2A {slug}", slug=slug, persona={"tone": "cercano"}, plan="premium")
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid,
        t=tid,
        ext=uniq("ig"),
    )
    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    if v3:
        await banderas_como_dueno(tid, aos_runtime="v3")
    catalogo = catalogo_de_acciones(_modules())
    agentes: dict[str, uuid.UUID] = {}
    async with tenant_scope(tid):
        for key in ("ventas", "agenda"):
            agente = await versiones.crear_agente(tid, key=key, kind="specialist", name=key.title())
            await versiones.publicar(tid, agente.id, catalogo=catalogo)
            await instancias.agregar_instancia(tid, cid, agente.id)
            agentes[key] = agente.id
    return {"tid": tid, "slug": slug, "cid": cid, **agentes}


def _enlace(desde: uuid.UUID, hacia: uuid.UUID, **kw: Any) -> dict[str, Any]:
    return {"from_agent_id": str(desde), "to_agent_id": str(hacia), "modes": ["delegate", "query"], **kw}


async def _publicar(tid: uuid.UUID, *enlaces: dict[str, Any]) -> Any:
    from core.agents import grafo_a2a
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await grafo_a2a.publicar(tid, list(enlaces))


async def _construir(tid: uuid.UUID, cid: uuid.UUID) -> Any:
    from core.releases.build import construir
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await construir(tid, cid, actor="test", modules=_modules())


async def _turno(n: dict[str, Any], release_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Cliente, conversación y cabecera del turno en `core_decisions`. (conversación, turno)."""
    tid, cid = n["tid"], n["cid"]
    cliente, conv, turno = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid,
        "insert into customers (id, tenant_id, channel, external_id) values (:id, :t, 'instagram', :ext)",
        id=cliente,
        t=tid,
        ext=uniq("cli"),
    )
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id, status) "
        "values (:id, :t, :c, 'instagram', :cc, 'open')",
        id=conv,
        t=tid,
        c=cliente,
        cc=cid,
    )
    await _sql(
        tid,
        "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_id, outcome, latency_ms) "
        "values (:id, :t, :cc, :conv, :r, 'routed', 5)",
        id=turno,
        t=tid,
        cc=cid,
        conv=conv,
        r=release_id,
    )
    return conv, turno


async def _release(tid: uuid.UUID, rid: uuid.UUID) -> Any:
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await cargar_release(session, tid, rid)


def _instancia(release: Any, agent_id: uuid.UUID) -> uuid.UUID:
    return next(c.instance_id for c in release.componentes if c.agent_id == agent_id)


# ------------------------------------------------------------------ grafo
async def test_publicar_es_idempotente_versiona_y_no_acepta_agentes_ajenos() -> None:
    from core.agents import grafo_a2a
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    otro = await _negocio()
    tid = n["tid"]
    # Dos altas concurrentes de la primera versión: una la crea, la otra la encuentra.
    async with tenant_scope(tid):
        resultados = await asyncio.gather(
            grafo_a2a.publicar(tid, [_enlace(n["ventas"], n["agenda"])]),
            grafo_a2a.publicar(tid, [_enlace(n["ventas"], n["agenda"])]),
        )
    assert sorted(c for _v, c in resultados) == [False, True]
    assert resultados[0][0].id == resultados[1][0].id
    v2, creada = await _publicar(tid, _enlace(n["ventas"], n["agenda"]), _enlace(n["agenda"], n["ventas"]))
    assert creada and v2.version == 2
    async with tenant_scope(tid):
        assert [v.version for v in await grafo_a2a.versiones(tid)] == [2, 1]
        vigente = await grafo_a2a.vigente(tid)
    assert vigente is not None and len(vigente.enlaces) == 2
    with pytest.raises(grafo_a2a.ErrorDelGrafo) as exc:
        await _publicar(tid, _enlace(n["ventas"], otro["agenda"]))
    assert exc.value.codigo == "agente_desconocido"


async def test_la_release_congela_el_grafo_solo_cuando_existe() -> None:
    from core.releases.build import ErrorDelBuild

    n = await _negocio()
    tid, cid = n["tid"], n["cid"]
    sin = await _construir(tid, cid)
    assert (await _construir(tid, cid)).release_id == sin.release_id  # sin cambios: la misma
    ((manifiesto, grafo_id),) = await _sql(
        tid, "select manifest, graph_version_id from connection_releases where id = :r", r=sin.release_id
    )
    assert "a2a_graph" not in manifiesto and grafo_id is None

    version, _ = await _publicar(tid, _enlace(n["ventas"], n["agenda"]))
    con = await _construir(tid, cid)
    assert con.creada and con.release_id != sin.release_id  # la dedupe no esconde el cambio
    ((manifiesto, grafo_id),) = await _sql(
        tid, "select manifest, graph_version_id from connection_releases where id = :r", r=con.release_id
    )
    assert manifiesto["a2a_graph"]["version_id"] == str(version.id) and grafo_id == version.id
    release = await _release(tid, con.release_id)
    assert release.a2a["content_hash"] == version.content_hash

    # El KernelSpec por defecto tiene maxDepth 3: un enlace con 5 hace fallar el build.
    await _publicar(tid, _enlace(n["ventas"], n["agenda"], max_depth=5))
    with pytest.raises(ErrorDelBuild) as exc:
        await _construir(tid, cid)
    assert exc.value.codigo == "a2a_invalido"


# ------------------------------------------------------------------ guarda y ledger
async def test_ciclo_queda_en_el_ledger_y_avisa_y_el_gasto_es_agent_run() -> None:
    from core.kernel import a2a
    from core.shared.events import ON_A2A_LOOP_DETECTED, event_bus
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    tid, cid = n["tid"], n["cid"]
    await _publicar(tid, _enlace(n["ventas"], n["agenda"]), _enlace(n["agenda"], n["ventas"]))
    rid = (await _construir(tid, cid)).release_id
    release = await _release(tid, rid)
    ventas, agenda = _instancia(release, n["ventas"]), _instancia(release, n["agenda"])
    conv, turno = await _turno(n, rid)
    avisos: list[dict[str, Any]] = []

    async def _oir(**payload: Any) -> None:
        avisos.append(payload)

    event_bus.subscribe(ON_A2A_LOOP_DETECTED, _oir)
    try:
        estado = a2a.EstadoDelTurno(tid, cid, conv, turno, release_id=rid)
        grafo, limites = a2a.GrafoCongelado.de_release(release), a2a.Limites.de_release(release)
        async with tenant_scope(tid):
            ida = await a2a.registrar(estado, a2a.Operacion(ventas, agenda, "query", "stock", {"sku": "X1"}), grafo, limites)
            vuelta = await a2a.registrar(estado, a2a.Operacion(agenda, ventas, "query", "precio", {"sku": "X1"}), grafo, limites)
            gasto = a2a.Gasto("modelo-test", 120, 30, Decimal("0.0042"), 800)
            await a2a.liquidar(estado, ida.call_id, ok=True, gasto=gasto, resultado={"stock": 3})
    finally:
        event_bus.unsubscribe_all([_oir])

    assert ida.permitido and vuelta.razon == "cycle"
    eventos = await _sql(
        tid,
        "select seq, type, reason, depth, payload_hash, payload_redacted from a2a_events e "
        "join a2a_conversations c on c.tenant_id = e.tenant_id and c.id = e.a2a_conversation_id "
        "where c.turn_id = :turno order by seq",
        turno=turno,
    )
    assert [(e[0], e[1], e[2]) for e in eventos] == [(1, "accepted", None), (2, "loop_detected", "cycle"), (3, "completed", None)]
    assert eventos[0][4] == ida.payload_hash and eventos[0][5] == {"sku": "string"}
    ((estado_a2a, llamadas, profundidad, gastado),) = await _sql(
        tid, "select status, calls, depth_reached, spent_usd from a2a_conversations where turn_id = :t", t=turno
    )
    assert (estado_a2a, llamadas, profundidad, gastado) == ("loop_blocked", 2, 2, Decimal("0.004200"))
    assert len(avisos) == 1 and avisos[0]["call_id"] == str(vuelta.call_id) and "sku" not in str(avisos[0])
    ((kind, call, instancia, costo),) = await _sql(
        tid, "select kind, a2a_call_id, agent_instance_id, cost_usd from usage_ledger where turn_id = :t", t=turno
    )
    assert (kind, call, instancia, costo) == ("agent_run", ida.call_id, agenda, Decimal("0.004200"))


async def test_sin_cabecera_del_turno_no_se_escribe_nada() -> None:
    from core.kernel import a2a
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    tid, cid = n["tid"], n["cid"]
    await _publicar(tid, _enlace(n["ventas"], n["agenda"]))
    rid = (await _construir(tid, cid)).release_id
    release = await _release(tid, rid)
    estado = a2a.EstadoDelTurno(tid, cid, None, None, release_id=rid)
    op = a2a.Operacion(_instancia(release, n["ventas"]), _instancia(release, n["agenda"]), "query", "stock")
    async with tenant_scope(tid):
        d = await a2a.registrar(estado, op, a2a.GrafoCongelado.de_release(release), a2a.Limites.de_release(release))
    assert not d.permitido and d.razon == "no_ledger"
    assert (await _sql(tid, "select count(*) from a2a_conversations"))[0][0] == 0


async def test_fk_compuestas_rechazan_referencias_de_otro_negocio() -> None:
    from core.kernel import a2a
    from core.shared.tenant import tenant_scope

    a = await _negocio()
    b = await _negocio()
    version_a, _ = await _publicar(a["tid"], _enlace(a["ventas"], a["agenda"]))
    rid = (await _construir(a["tid"], a["cid"])).release_id
    release = await _release(a["tid"], rid)
    conv, turno = await _turno(a, rid)
    ventas, agenda = _instancia(release, a["ventas"]), _instancia(release, a["agenda"])
    estado = a2a.EstadoDelTurno(a["tid"], a["cid"], conv, turno, release_id=rid)
    async with tenant_scope(a["tid"]):
        await a2a.registrar(estado, a2a.Operacion(ventas, agenda, "query", "stock"), a2a.GrafoCongelado.de_release(release), a2a.Limites())
    # Desde B, nada puede apuntar a filas de A.
    casos = [
        "insert into agent_links (tenant_id, graph_version_id, from_agent_id, to_agent_id, modes) "
        "values (:t, :gv, :va, :ag, ARRAY['query'])",
        "insert into a2a_conversations (tenant_id, connection_id, conversation_id, turn_id, root_mode, "
        "initiator_instance_id, max_depth, budget_usd, budget_tokens, deadline_at) "
        "values (:t, :cc, :conv, :turno, 'query', :vi, 3, 0.05, 40000, now())",
        "insert into a2a_events (tenant_id, a2a_conversation_id, seq, call_id, type, mode, depth, "
        "from_instance_id, intent) values (:t, :a2a, 9, gen_random_uuid(), 'denied', 'query', 1, :vi, 'x')",
    ]
    ((a2a_conv,),) = await _sql(a["tid"], "select id from a2a_conversations where turn_id = :t", t=turno)
    params = {
        "t": b["tid"], "va": a["ventas"], "ag": a["agenda"], "cc": a["cid"], "conv": conv,
        "turno": turno, "vi": ventas, "a2a": a2a_conv, "gv": version_a.id,
    }
    for sql in casos:
        with pytest.raises(DBAPIError):
            await _sql(b["tid"], sql, **params)


# ------------------------------------------------------------------ API
async def test_api_del_grafo_y_de_las_llamadas(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    n = await _negocio()
    master = await api_headers(app_client, n["tid"])
    r = await app_client.put(
        "/api/v1/admin/a2a/graph", headers=master, json={"links": [_enlace(n["ventas"], n["agenda"], modes=["broadcast"])]}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "modo_no_permitido", r.text
    r = await app_client.put(
        "/api/v1/admin/a2a/graph", headers=master, json={"links": [_enlace(n["ventas"], n["ventas"])]}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "autollamada", r.text
    cuerpo = {"links": [_enlace(n["ventas"], n["agenda"], modes=["handoff"]), _enlace(n["agenda"], n["ventas"], modes=["handoff"])]}
    r = await app_client.put("/api/v1/admin/a2a/graph", headers=master, json=cuerpo)
    assert r.status_code == 200 and r.json()["created"] is True, r.text
    assert len(r.json()["warnings"]) == 1 and r.json()["warnings"][0].startswith("ciclo_handoff:")
    r = await app_client.put("/api/v1/admin/a2a/graph", headers=master, json=cuerpo)
    assert r.json()["created"] is False
    r = await app_client.get("/api/v1/admin/a2a/graph", headers=master)
    assert r.status_code == 200 and r.json()["version"]["version"] == 1 and len(r.json()["links"]) == 2
    r = await app_client.get(f"/api/v1/admin/a2a/calls?connection_id={n['cid']}", headers=master)
    assert r.status_code == 200 and r.json() == []

    # En legacy, quien no es master no publica.
    legacy = await _negocio(v3=False)
    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=legacy["tid"], admin_id=admin.id, role="owner"))
    dueno = await api_headers(app_client, legacy["tid"], email=email, password=PASSWORD)
    r = await app_client.put(
        "/api/v1/admin/a2a/graph", headers=dueno, json={"links": [_enlace(legacy["ventas"], legacy["agenda"])]}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "aos_runtime_legacy", r.text


async def test_las_llamadas_se_agrupan_por_call_id_y_no_cruzan_negocios(app_client) -> None:
    from core.kernel import a2a
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    otro = await _negocio()
    tid, cid = n["tid"], n["cid"]
    await _publicar(tid, _enlace(n["ventas"], n["agenda"]))
    rid = (await _construir(tid, cid)).release_id
    release = await _release(tid, rid)
    conv, turno = await _turno(n, rid)
    estado = a2a.EstadoDelTurno(tid, cid, conv, turno, release_id=rid)
    op = a2a.Operacion(_instancia(release, n["ventas"]), _instancia(release, n["agenda"]), "query", "stock")
    async with tenant_scope(tid):
        d = await a2a.registrar(estado, op, a2a.GrafoCongelado.de_release(release), a2a.Limites())
        await a2a.liquidar(estado, d.call_id, ok=False)
    r = await app_client.get(f"/api/v1/admin/a2a/calls?connection_id={cid}", headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    filas = r.json()
    assert len(filas) == 1 and filas[0]["call_id"] == str(d.call_id)
    assert filas[0]["status"] == "failed" and filas[0]["reason"] == "error" and filas[0]["intent"] == "stock"
    # Con la cabecera de otro negocio, la misma conexión no devuelve nada (RLS).
    r = await app_client.get(f"/api/v1/admin/a2a/calls?connection_id={cid}", headers=await api_headers(app_client, otro["tid"]))
    assert r.status_code == 200 and r.json() == []

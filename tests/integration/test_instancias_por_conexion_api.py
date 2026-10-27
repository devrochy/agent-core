"""HU-AOS-13 (parte 2) · escrituras de la API de instancias y altas posteriores (SPEC-26).

- **API** (`/api/v1/admin/connections/{id}/agents`): agregar un especialista, habilitar o
  deshabilitar, cambiar el agente por defecto y quitar. El Coordinador lo gestiona el sistema
  (exactamente uno por conexión, ADR-0112 §1): no se agrega, no se deshabilita ni se quita por
  la API. Siempre hay un agente por defecto, habilitado. 404 entre negocios; 403 con
  `aos_runtime = legacy` para quien no es master.
- **Altas posteriores** (ADR-0115 §6, los cinco caminos de ADR-0122 §6 por
  `numero_para_conexion`): el número nuevo de un negocio con agentes nace con Coordinador y
  General, en la misma transacción; sin agentes no se crea nada (lo hará el backfill). Religar
  un número no recrea lo que el dueño quitó. Un error en las instancias no tumba el alta.
- **Promoción** (ADR-0127 §3): la conexión del número real nace con las instancias clonadas de
  la de prueba; `clonar_instancias` devuelve el mapa por agente (contrato con HU-AOS-14).

Sin LLM ni red (Graph falso).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, banderas_como_dueno, uniq
from test_agentes_versionados import PASSWORD, _dueno, spec_en_su_enfoque
from test_backfill_agentes import _conexion, _legacy_y_settings, _modules, _sql
from test_byok_produccion import GraphFalso, _alta, _byok_de_prueba, _instalar, _negocio, _num, _url

pytestmark = [
    pytest.mark.aislamiento,  # escrituras de la API entre negocios y altas (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


# ---------------------------------------------------------------- helpers
async def _con_agentes() -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID]:
    """Negocio con su BYOK de prueba migrado al Agent OS (backfill de conexiones y de agentes).
    Devuelve (negocio, cuenta de prueba, número de prueba, su conexión canónica)."""
    from core.agents.backfill_agentes import backfill_agentes
    from core.gateway.conexion_canonica_backfill import backfill_conexiones

    tid = await _negocio()
    cuenta, _app, phone = await _byok_de_prueba(tid)
    await backfill_conexiones(dry_run=False, tenant_id=tid)
    assert not (await backfill_agentes(dry_run=False, tenant_id=tid, modules=_modules())).fallo
    # Premium (5 agentes publicados, ADR-0142): estos tests suman especialistas; el tope del plan
    # se prueba en `test_planes_por_agentes.py`.
    await _sql(tid, "update tenant_subscriptions set plan_id = 'premium' where tenant_id = :t", t=tid)
    return tid, cuenta, phone, await _canonica(tid, phone)


async def _canonica(tid: uuid.UUID, phone: str) -> uuid.UUID:
    (fila,) = await _sql(
        tid,
        "select id from channel_connections where tenant_id = :t and provider = 'meta' and external_id = :p "
        "and deleted_at is null",
        t=tid, p=phone,
    )
    return fila[0]


async def _instancias(tid: uuid.UUID, cid: uuid.UUID) -> list[tuple[str, str, bool, bool, int]]:
    """(key, rol, por defecto, habilitada, prioridad) de las instancias vivas, por key."""
    return [
        tuple(f)
        for f in await _sql(
            tid,
            "select a.key, i.role, i.is_default, i.enabled, i.priority from agent_instances i "
            "join agents a on a.tenant_id = i.tenant_id and a.id = i.agent_id "
            "where i.tenant_id = :t and i.connection_id = :c and i.deleted_at is null order by a.key",
            t=tid, c=cid,
        )
    ]


async def _especialista(app_client, h: dict[str, str], key: str, *, publicar: bool = True) -> str:
    r = await app_client.post("/api/v1/admin/agents", headers=h, json={"key": key, "kind": "specialist", "name": key})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    if publicar:
        r = await app_client.put(
            f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": spec_en_su_enfoque(key), "lock_version": 0}
        )
        assert r.status_code == 200, r.text
        r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={})
        assert r.status_code == 200, r.text
    return aid


def _agents(cid: Any, iid: Any = None) -> str:
    return f"/api/v1/admin/connections/{cid}/agents" + (f"/{iid}" if iid else "")


def _codigo(r: Any) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


# ---------------------------------------------------------------- API
async def test_api_agrega_cambia_el_por_defecto_y_quita_sin_romper_uno_y_solo_uno(app_client) -> None:
    tid, _cuenta, _phone, cid = await _con_agentes()
    h = await api_headers(app_client, tid)  # master
    antes = await _legacy_y_settings(tid)
    ventas = await _especialista(app_client, h, "ventas")
    sin_version = await _especialista(app_client, h, "soporte", publicar=False)
    ids = {f["key"]: f["id"] for f in (await app_client.get(_agents(cid), headers=h)).json()}

    # Agregar un especialista.
    r = await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas, "priority": 50})
    assert r.status_code == 201, r.text
    assert (r.json()["key"], r.json()["role"], r.json()["is_default"], r.json()["priority"]) == (
        "ventas", "specialist", False, 50,
    )
    ids["ventas"] = r.json()["id"]
    assert _codigo(await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas})) == (409, "already_assigned")
    assert _codigo(await app_client.post(_agents(cid), headers=h, json={"agent_id": sin_version})) == (
        409, "agent_unpublished",
    )
    # El Coordinador lo pone el sistema: no se agrega otro (ni el mismo) por la API.
    (coordinador,) = await _sql(tid, "select id from agents where tenant_id = :t and key = 'coordinador'", t=tid)
    r = await app_client.post(_agents(cid), headers=h, json={"agent_id": str(coordinador[0])})
    assert _codigo(r) == (409, "not_specialist")
    # Sin asignación masiva: el rol, la conexión o el negocio no van en el cuerpo.
    r = await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas, "role": "core"})
    assert r.status_code == 422

    # Cambiar el agente por defecto: queda exactamente uno.
    r = await app_client.patch(_agents(cid, ids["ventas"]), headers=h, json={"is_default": True})
    assert r.status_code == 200 and r.json()["is_default"] is True, r.text
    assert await _instancias(tid, cid) == [
        ("coordinador", "core", False, True, 100),
        ("general", "specialist", False, True, 100),
        ("ventas", "specialist", True, True, 50),
    ]
    # Quitar el "por defecto" sin elegir otro no existe.
    r = await app_client.patch(_agents(cid, ids["ventas"]), headers=h, json={"is_default": False})
    assert r.status_code == 422

    # Habilitar / deshabilitar: el por defecto siempre habilitado.
    r = await app_client.patch(_agents(cid, ids["ventas"]), headers=h, json={"enabled": False})
    assert _codigo(r) == (409, "default_disabled")
    r = await app_client.patch(_agents(cid, ids["general"]), headers=h, json={"enabled": False, "priority": 7})
    assert r.status_code == 200 and (r.json()["enabled"], r.json()["priority"]) == (False, 7), r.text
    assert _codigo(await app_client.patch(_agents(cid, ids["general"]), headers=h, json={"is_default": True})) == (
        409, "default_disabled",
    )
    r = await app_client.patch(_agents(cid, ids["general"]), headers=h, json={"enabled": True, "is_default": True})
    assert r.status_code == 200 and r.json()["is_default"] is True, r.text

    # Uno y solo uno: el Coordinador no se deshabilita, no es "por defecto" y no se quita.
    assert _codigo(await app_client.patch(_agents(cid, ids["coordinador"]), headers=h, json={"enabled": False})) == (
        409, "core_required",
    )
    assert _codigo(await app_client.patch(_agents(cid, ids["coordinador"]), headers=h, json={"is_default": True})) == (
        409, "not_specialist",
    )
    assert _codigo(await app_client.delete(_agents(cid, ids["coordinador"]), headers=h)) == (409, "core_required")
    assert _codigo(await app_client.delete(_agents(cid, ids["general"]), headers=h)) == (409, "default_required")

    # Quitar un especialista es borrado lógico; puede volver a agregarse.
    r = await app_client.delete(_agents(cid, ids["ventas"]), headers=h)
    assert r.status_code == 204, r.text
    assert [f["key"] for f in (await app_client.get(_agents(cid), headers=h)).json()] == ["coordinador", "general"]
    (borrada,) = await _sql(tid, "select deleted_at is not null from agent_instances where id = :i", i=ids["ventas"])
    assert borrada[0] is True
    assert _codigo(await app_client.patch(_agents(cid, ids["ventas"]), headers=h, json={"priority": 1})) == (
        404, "not_found",
    )
    r = await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas})
    assert r.status_code == 201 and r.json()["id"] != ids["ventas"], r.text

    # Con `legacy` el loop no lee nada de esto: lo que lee queda igual.
    assert await _legacy_y_settings(tid) == antes


async def test_api_rechaza_integraciones_y_conexiones_sin_coordinador(app_client) -> None:
    tid, _cuenta, _phone, _cid = await _con_agentes()
    h = await api_headers(app_client, tid)
    ventas = await _especialista(app_client, h, "ventas")
    calendario = await _conexion(tid, "google_calendar", channel="calendar", provider="google")
    r = await app_client.post(_agents(calendario), headers=h, json={"agent_id": ventas})
    assert _codigo(r) == (422, "connection_not_conversational")
    # Una conexión nueva sin backfill todavía no tiene Coordinador: no se le suman especialistas.
    nueva = await _conexion(tid, "instagram_meta", channel="instagram")
    assert _codigo(await app_client.post(_agents(nueva), headers=h, json={"agent_id": ventas})) == (409, "core_missing")
    assert await _instancias(tid, nueva) == []


async def test_api_no_cruza_negocios(app_client) -> None:
    tid, _c, _p, cid = await _con_agentes()
    otro, _c2, _p2, cid_otro = await _con_agentes()
    h = await api_headers(app_client, tid)
    h_otro = await api_headers(app_client, otro)
    ventas = await _especialista(app_client, h, "ventas")
    ventas_otro = await _especialista(app_client, h_otro, "ventas")
    instancia_otro = (await app_client.get(_agents(cid_otro), headers=h_otro)).json()[1]["id"]

    # La conexión de otro negocio no existe para este.
    assert _codigo(await app_client.post(_agents(cid_otro), headers=h, json={"agent_id": ventas})) == (404, "not_found")
    assert _codigo(await app_client.patch(_agents(cid_otro, instancia_otro), headers=h, json={"priority": 1})) == (
        404, "not_found",
    )
    assert _codigo(await app_client.delete(_agents(cid_otro, instancia_otro), headers=h)) == (404, "not_found")
    # Ni su instancia por la conexión propia, ni su agente.
    assert _codigo(await app_client.patch(_agents(cid, instancia_otro), headers=h, json={"priority": 1})) == (
        404, "not_found",
    )
    assert _codigo(await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas_otro})) == (404, "not_found")
    # Nada cambió en el otro negocio.
    assert await _instancias(otro, cid_otro) == [
        ("coordinador", "core", False, True, 100),
        ("general", "specialist", True, True, 100),
    ]


async def test_con_runtime_legacy_solo_el_master_escribe_instancias(app_client) -> None:
    tid, _c, _p, cid = await _con_agentes()
    ventas = await _especialista(app_client, await api_headers(app_client, tid), "ventas")
    h = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    assert _codigo(await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas})) == (
        403, "aos_runtime_legacy",
    )
    assert (await app_client.get(_agents(cid), headers=h)).status_code == 200

    await banderas_como_dueno(tid, aos_runtime="v3")
    r = await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas})
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- altas posteriores
async def test_un_numero_nuevo_nace_con_coordinador_y_general_y_sin_agentes_no_se_crea_nada(
    app_client, monkeypatch
) -> None:
    tid, _cuenta, _phone, _cid = await _con_agentes()
    antes = await _legacy_y_settings(tid)
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "CLOUD_API", "account_mode": "SANDBOX"}
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", json=_alta(app_id, waba_id, phone_id), headers=await api_headers(app_client, tid)
    )
    assert r.status_code == 201, r.text
    assert await _instancias(tid, await _canonica(tid, phone_id)) == [
        ("coordinador", "core", False, True, 100),
        ("general", "specialist", True, True, 100),
    ]
    assert await _legacy_y_settings(tid) == antes

    # Un negocio sin agentes (sin backfill): el alta sigue igual y no crea instancias.
    nuevo = await _negocio()
    app2, waba2, phone2 = uniq("app"), uniq("waba"), uniq("ph")
    graph2 = GraphFalso(app_id=app2, wabas={waba2: [phone2]})
    graph2.numeros[phone2] = {"platform_type": "CLOUD_API", "account_mode": "SANDBOX"}
    _instalar(monkeypatch, graph2)
    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", json=_alta(app2, waba2, phone2), headers=await api_headers(app_client, nuevo)
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "connected"
    assert await _instancias(nuevo, await _canonica(nuevo, phone2)) == []


async def test_religar_el_numero_no_recrea_lo_que_el_dueno_quito(app_client) -> None:
    from core.agents.backfill_agentes import backfill_agentes
    from core.db.session import get_session
    from core.gateway.meta_asset_claims import numero_para_conexion
    from core.shared.tenant import tenant_scope

    tid, cuenta, phone, cid = await _con_agentes()
    h = await api_headers(app_client, tid)
    ventas = await _especialista(app_client, h, "ventas")
    r = await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas, "is_default": True})
    assert r.status_code == 201, r.text
    general = next(f["id"] for f in (await app_client.get(_agents(cid), headers=h)).json() if f["key"] == "general")
    assert (await app_client.delete(_agents(cid, general), headers=h)).status_code == 204
    esperado = [("coordinador", "core", False, True, 100), ("ventas", "specialist", True, True, 100)]

    # Reconectar el número (pasa por numero_para_conexion) y otra corrida del backfill.
    async with tenant_scope(tid):
        async with get_session() as session:
            await numero_para_conexion(session, tid, cuenta, phone)
    reporte = await backfill_agentes(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo
    assert reporte.instancias["configurada"] >= 1
    assert await _instancias(tid, cid) == esperado


async def test_un_error_en_las_instancias_no_tumba_el_alta(app_client, monkeypatch) -> None:
    import core.agents.instancias as instancias_mod

    tid, _cuenta, _phone, _cid = await _con_agentes()

    async def _rompe(session, *_args, **_kwargs):
        # Un error de BD real dentro del savepoint: la transacción del alta sigue usable.
        from sqlalchemy import text

        await session.execute(text("select 1 / 0"))

    monkeypatch.setattr(instancias_mod, "_inicializar_al_alta", _rompe)
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {
        "platform_type": "CLOUD_API", "account_mode": "SANDBOX", "display_phone_number": "+57 300 111",
    }
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", json=_alta(app_id, waba_id, phone_id), headers=await api_headers(app_client, tid)
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "connected"
    # El número quedó bien guardado (el savepoint no expira lo que el alta ya escribió).
    (fila,) = await _sql(
        tid, "select display_phone_number, registered from phone_numbers where tenant_id = :t and phone_number_id = :p",
        t=tid, p=phone_id,
    )
    assert tuple(fila) == ("+57 300 111", True)
    assert await _instancias(tid, await _canonica(tid, phone_id)) == []


# ---------------------------------------------------------------- promoción
async def test_la_promocion_clona_las_instancias_de_la_prueba(app_client, monkeypatch) -> None:
    tid, cuenta, _phone, cid = await _con_agentes()
    h = await api_headers(app_client, tid)
    ventas = await _especialista(app_client, h, "ventas")
    r = await app_client.post(_agents(cid), headers=h, json={"agent_id": ventas, "priority": 30, "is_default": True})
    assert r.status_code == 201, r.text
    general = next(f["id"] for f in (await app_client.get(_agents(cid), headers=h)).json() if f["key"] == "general")
    assert (await app_client.patch(_agents(cid, general), headers=h, json={"enabled": False})).status_code == 200

    app_id = (await _sql(tid, "select meta_app_id from meta_connections where id = :c", c=cuenta))[0][0]
    waba_real, phone_real = _num(), _num()
    graph = GraphFalso(app_id=app_id, wabas={waba_real: [phone_real]})
    graph.numeros[phone_real] = {"platform_type": "CLOUD_API", "account_mode": "LIVE"}
    _instalar(monkeypatch, graph)
    r = await app_client.post(
        _url(cuenta, "/number"), json={"waba_id": waba_real, "phone_number_id": phone_real}, headers=h
    )
    assert r.status_code == 200, r.text

    real = await _canonica(tid, phone_real)
    assert real != cid
    assert await _instancias(tid, real) == await _instancias(tid, cid) == [
        ("coordinador", "core", False, True, 100),
        ("general", "specialist", False, False, 100),
        ("ventas", "specialist", True, True, 30),
    ]
    # Reintentar el paso 1 no duplica nada.
    r = await app_client.post(
        _url(cuenta, "/number"), json={"waba_id": waba_real, "phone_number_id": phone_real}, headers=h
    )
    assert r.status_code == 200, r.text
    assert len(await _instancias(tid, real)) == 3


async def test_clonar_instancias_mapea_solo_pares_del_mismo_agente() -> None:
    from core.agents.instancias import clonar_instancias
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, _cuenta, _phone, origen = await _con_agentes()
    destino = await _conexion(tid, "instagram_meta", channel="instagram")
    async with tenant_scope(tid):
        async with get_session() as session:
            primero = await clonar_instancias(session, tid, origen, destino)
    assert primero.creadas == 2 and not primero.omitidas
    pares = await _sql(
        tid,
        "select o.id, d.id from agent_instances o join agent_instances d on d.agent_id = o.agent_id "
        "and d.tenant_id = o.tenant_id where o.connection_id = :o and d.connection_id = :d",
        o=origen, d=destino,
    )
    assert primero.mapa == {o: d for o, d in pares}

    # El destino ya configurado no recibe nada nuevo, pero el mapa sigue dando los pares.
    otro_agente = uuid.uuid4()
    await _sql(
        tid,
        "insert into agents (id, tenant_id, key, kind, name) values (:a, :t, :k, 'specialist', 'Extra')",
        a=otro_agente, t=tid, k=uniq("extra").replace("-", "_")[:40],
    )
    (extra,) = await _sql(
        tid,
        "insert into agent_instances (tenant_id, connection_id, agent_id, role) values (:t, :c, :a, 'specialist') "
        "returning id",
        t=tid, c=origen, a=otro_agente,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            segundo = await clonar_instancias(session, tid, origen, destino)
    assert segundo.creadas == 0
    assert segundo.mapa == primero.mapa
    assert segundo.omitidas == [(extra[0], "destino_configurado")]


# ---------------------------------------------------------------- revisiones (seguridad y aislamiento)
async def test_choque_con_un_indice_unico_se_traduce_por_nombre() -> None:
    from sqlalchemy.exc import IntegrityError

    from core.agents.instancias import _conflicto_por_indice

    tid, _c, _p, cid = await _con_agentes()
    (general,) = await _sql(tid, "select id from agents where tenant_id = :t and key = 'general'", t=tid)
    with pytest.raises(IntegrityError) as exc:
        await _sql(
            tid,
            "insert into agent_instances (tenant_id, connection_id, agent_id, role) values (:t, :c, :a, 'specialist')",
            t=tid, c=cid, a=general[0],
        )
    conflicto = _conflicto_por_indice(exc.value)
    assert conflicto is not None and conflicto.code == "already_assigned"


async def test_las_funciones_de_instancias_no_cruzan_negocios() -> None:
    from core.agents.instancias import (
        ConexionNoEncontrada,
        agregar_instancia,
        asegurar_instancias_base,
        clonar_instancias,
        instancias_al_alta,
    )
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, _c, phone, cid = await _con_agentes()
    otro, _c2, _p2, cid_otro = await _con_agentes()
    destino = await _conexion(tid, "instagram_meta", channel="instagram")
    antes_otro = await _instancias(otro, cid_otro)

    async with tenant_scope(tid):
        # Origen de otro negocio: no existe para este (RLS y filtro por tenant_id).
        async with get_session() as session:
            with pytest.raises(ConexionNoEncontrada):
                await clonar_instancias(session, tid, cid_otro, destino)
        # El negocio del scope manda: pedir otro negocio dentro de este scope falla cerrado.
        async with get_session() as session:
            with pytest.raises(PermissionError):
                await asegurar_instancias_base(session, otro, cid_otro)
            with pytest.raises(PermissionError):
                await clonar_instancias(session, otro, cid_otro, destino)
            with pytest.raises(PermissionError):
                await instancias_al_alta(session, otro, phone, uuid.uuid4())
        with pytest.raises(PermissionError):
            await agregar_instancia(otro, cid_otro, uuid.uuid4())
        # Clonar desde una conexión ajena en el alta: no entra nada y el alta seguiría.
        cuenta = (await _sql(tid, "select meta_connection_id from channel_connections where id = :c", c=cid))[0][0]
        await _sql(tid, "update agent_instances set deleted_at = now() where connection_id = :c", c=cid)
        async with get_session() as session:
            await instancias_al_alta(session, tid, phone, cuenta, instancias_de=cid_otro)
    assert await _instancias(tid, destino) == []
    assert await _instancias(tid, cid) == []
    assert await _instancias(otro, cid_otro) == antes_otro


async def test_las_escrituras_piden_ver_la_conexion_y_no_cruzan_negocios_como_dueno(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission
    from core.db.session import get_session

    tid, _c, _p, cid = await _con_agentes()
    otro, _c2, _p2, cid_otro = await _con_agentes()
    for negocio in (tid, otro):
        await banderas_como_dueno(negocio, aos_runtime="v3")
    ventas = await _especialista(app_client, await api_headers(app_client, tid), "ventas")

    # Un dueño de este negocio (v3) no alcanza la conexión del otro.
    h = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    assert _codigo(await app_client.post(_agents(cid_otro), headers=h, json={"agent_id": ventas})) == (404, "not_found")
    instancia_otro = (await _instancias_ids(otro, cid_otro))[0]
    assert _codigo(await app_client.delete(_agents(cid_otro, instancia_otro), headers=h)) == (404, "not_found")

    # `settings` con escritura sin poder ver `connections`: 403.
    email = f"{uniq('solo-settings')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(AdminPermission(admin_id=admin.id, section="settings", access="write", tenant_id=None))
    h2 = await api_headers(app_client, tid, email=email, password=PASSWORD)
    r = await app_client.post(_agents(cid), headers=h2, json={"agent_id": ventas})
    assert r.status_code == 403, r.text
    assert len(await _instancias(tid, cid)) == 2


async def _instancias_ids(tid: uuid.UUID, cid: uuid.UUID) -> list[uuid.UUID]:
    return [f[0] for f in await _sql(tid, "select id from agent_instances where connection_id = :c", c=cid)]

"""HU-AOS-23 · consola de conexiones (`GET /api/v1/admin/connections/console`, SPEC-26).

- Por conexión: Coordinador y agentes con la versión de la release activa, release, salud,
  alcance de la pausa y consumo de 7 días; la bandera `aos_routing` de solo lectura.
- D-35: los agentes publicados sin conexión salen en `unassigned_agents`.
- Permisos (ADR-0136: el colaborador lee todo salvo denegación `none`): 403 sin `connections`; `can_edit` solo con escritura; `cost_usd` solo con `costs`.
- Aislamiento: nada de otro negocio aparece.
- La pausa de la cuenta de Meta queda en `audit_log`.

Sin LLM ni red.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers, uniq
from test_agentes_versionados import PASSWORD
from test_backfill_agentes import _conexion, _sql
from test_instancias_por_conexion_api import _con_agentes, _especialista

pytestmark = [
    pytest.mark.aislamiento,  # lectura nueva por negocio (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CONSOLA = "/api/v1/admin/connections/console"


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
            session.add(AdminPermission(admin_id=admin.id, section=seccion, access=acceso, tenant_id=tid))
    return email


async def test_consola_muestra_coordinador_agentes_release_salud_y_consumo(app_client) -> None:
    tid, cuenta, _phone, cid = await _con_agentes()
    h = await api_headers(app_client, tid)  # master
    await _sql(None, "update tenants set aos_routing = 'off' where id = :t", t=tid)

    # Release 1 activa con Coordinador y General; luego se suma Ventas (pendiente de release).
    r = await app_client.post(f"/api/v1/admin/connections/{cid}/releases", headers=h, json={})
    assert r.status_code in (200, 201), r.text
    rid = r.json()["release"]["id"]
    r = await app_client.post(f"/api/v1/admin/connections/{cid}/releases/{rid}/promote", headers=h, json={})
    assert r.status_code == 200, r.text
    ventas = await _especialista(app_client, h, "ventas")
    assert (await app_client.post(f"/api/v1/admin/connections/{cid}/agents", headers=h, json={"agent_id": ventas})).status_code == 201
    huerfano = await _especialista(app_client, h, "cobranza")  # publicado, sin conexión (D-35)
    await _especialista(app_client, h, "borrador", publicar=False)  # sin versión: no cuenta

    # Consumo: las filas de los últimos 7 días (sin turno: cada fila es uno); la de hace 8 no cuenta.
    for dias, costo in ((0, 0.01), (0, 0.02), (8, 5.0)):
        await _sql(
            tid,
            "insert into usage_ledger (id, tenant_id, model, input_tokens, output_tokens, cost_usd, connection_id, "
            "turn_id, created_at) values (gen_random_uuid(), :t, 'm', 10, 5, :c, :cid, null, now() - make_interval(days => :d))",
            t=tid, c=costo, cid=cid, d=dias,
        )
    integracion = await _conexion(tid, "google_calendar", channel="calendar", provider="google")

    r = await app_client.get(CONSOLA, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["runtime"], body["routing"], body["can_edit"]) == ("legacy", "off", True)
    assert [a["key"] for a in body["unassigned_agents"]] == ["cobranza"]
    assert body["unassigned_agents"][0]["id"] == huerfano
    filas = {f["id"]: f for f in body["items"]}
    assert body["items"][-1]["id"] == str(integracion)  # integraciones al final
    assert filas[str(integracion)]["conversational"] is False

    fila = filas[str(cid)]
    assert fila["meta_connection_id"] == str(cuenta)
    assert (fila["health"], fila["pause_scope"], fila["pausable"]) == ("ok", "meta_account", True)
    assert fila["coordinator"]["key"] == "coordinador"
    assert fila["coordinator"]["release_version"] == fila["coordinator"]["latest_version"] is not None
    agentes = {a["key"]: a for a in fila["agents"]}
    assert agentes["general"]["is_default"] is True and agentes["general"]["release_version"] is not None
    assert agentes["ventas"]["release_version"] is None  # aplica con la próxima release
    assert agentes["ventas"]["latest_version"] == 1
    assert fila["active_release"]["number"] == 1 and fila["canary_release"] is None
    assert fila["usage_7d"] == {
        "turns": 2, "conversations": 0, "input_tokens": 20, "output_tokens": 10, "cost_usd": 0.03,
    }

    # Con la cuenta pausada (ADR-0122) la salud lo dice.
    await _sql(tid, "update meta_connections set status = 'revoked' where id = :m", m=cuenta)
    fila = {f["id"]: f for f in (await app_client.get(CONSOLA, headers=h)).json()["items"]}[str(cid)]
    assert fila["health"] == "paused"

    # Paginada.
    r = await app_client.get(CONSOLA, headers=h, params={"page": 1, "page_size": 1})
    assert (len(r.json()["items"]), r.json()["total"], r.json()["has_more"]) == (1, 2, True)


async def test_consola_permisos_y_aislamiento(app_client) -> None:
    tid, _c, _p, cid = await _con_agentes()
    otro, _c2, _p2, cid_otro = await _con_agentes()
    await _especialista(app_client, await api_headers(app_client, otro), "ajeno")

    solo_ver = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"connections": "read", "costs": "none"}), password=PASSWORD
    )
    r = await app_client.get(CONSOLA, headers=solo_ver)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_edit"] is False
    assert [f["id"] for f in body["items"]] == [str(cid)]
    assert str(cid_otro) not in r.text and "ajeno" not in r.text
    assert body["items"][0]["usage_7d"]["cost_usd"] is None  # sin `costs`

    con_costos = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"connections": "write", "costs": "read"}), password=PASSWORD
    )
    body = (await app_client.get(CONSOLA, headers=con_costos)).json()
    assert body["can_edit"] is True and body["items"][0]["usage_7d"]["cost_usd"] == 0

    sin_seccion = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"connections": "none", "settings": "write"}), password=PASSWORD
    )
    assert (await app_client.get(CONSOLA, headers=sin_seccion)).status_code == 403


async def test_pausar_la_cuenta_de_meta_queda_auditado(app_client) -> None:
    tid, cuenta, _p, _cid = await _con_agentes()
    h = await api_headers(app_client, tid)
    r = await app_client.post(f"/api/v1/admin/meta/connections/{cuenta}/action", headers=h, json={"action": "deactivate"})
    assert r.status_code == 204, r.text
    filas = await _sql(
        tid,
        "select action, input->>'connection_id' from audit_log where tenant_id = :t and action like 'meta_connection.%'",
        t=tid,
    )
    assert [tuple(f) for f in filas] == [("meta_connection.paused", str(cuenta))]
    (estado,) = await _sql(tid, "select status from meta_connections where id = :m", m=cuenta)
    assert estado[0] == "revoked"


async def test_desactivar_una_conexion_manual_queda_auditado(app_client) -> None:
    tid, _c, _p, _cid = await _con_agentes()
    manual = await _conexion(tid, None, channel="telegram", provider="telegram")
    h = await api_headers(app_client, tid)
    r = await app_client.post(f"/api/v1/admin/connections/{manual}/action", headers=h, json={"action": "deactivate"})
    assert r.status_code == 204, r.text
    filas = await _sql(
        tid, "select action, input->>'connection_id' from audit_log where tenant_id = :t and action like 'connection.%'", t=tid
    )
    assert [tuple(f) for f in filas] == [("connection.paused", str(manual))]

"""La conexión conversacional nueva nace con su release 1 (ADR-0191, HU-AOS-69).

- Al crear la conexión (aquí, el chat web desde la API) con el Coordinador y el General del negocio,
  la misma transacción deja un evento `connection.release_1` en el outbox.
- El handler construye y promueve la release 1 de esa conexión: después, la conexión tiene release
  activa y el turno ya no falla cerrado por falta de release.
- Es idempotente: entregarlo dos veces no crea otra release.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers
from test_backfill_agentes import _sql
from test_chat_web_conexion import _webchat
from test_instancias_por_conexion_api import _con_agentes

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SETTINGS = "/api/v1/admin/settings/web-chat"


async def _releases(tid: uuid.UUID, cid: uuid.UUID) -> list[tuple[int, bool]]:
    filas = await _sql(
        tid,
        "select r.number, (c.active_release_id = r.id) from connection_releases r "
        "join channel_connections c on c.id = r.connection_id where r.connection_id = :c order by r.number",
        c=cid,
    )
    return [(int(n), bool(a)) for n, a in filas]


async def test_el_chat_web_nuevo_pide_y_recibe_su_release_1(app_client) -> None:
    from core.events.core_handlers import on_connection_release_1
    from core.shared.tenant import tenant_scope

    tid, _cuenta, _phone, _cid = await _con_agentes()
    r = await app_client.put(
        SETTINGS, headers=await api_headers(app_client, tid), json={"enabled": False, "allowed_origins": []}
    )
    assert r.status_code == 200, r.text
    (widget,) = await _webchat(tid)
    eventos = await _sql(
        tid,
        "select payload->>'connection_id', status from domain_events where tenant_id = :t and topic = 'connection.release_1' "
        "and dedupe_key = :k",
        t=tid, k=f"release_1:{widget.id}",
    )
    assert eventos == [(str(widget.id), "pendiente")]
    assert await _releases(tid, widget.id) == []

    async with tenant_scope(tid):
        await on_connection_release_1(connection_id=str(widget.id))
    assert await _releases(tid, widget.id) == [(1, True)]

    async with tenant_scope(tid):
        await on_connection_release_1(connection_id=str(widget.id))  # at-least-once: no crea otra
    assert await _releases(tid, widget.id) == [(1, True)]


async def test_el_handler_fuera_del_negocio_falla_sin_tocar_nada() -> None:
    from core.events.core_handlers import on_connection_release_1

    with pytest.raises(RuntimeError, match="tenant_scope"):
        await on_connection_release_1(connection_id=str(uuid.uuid4()))


async def test_sin_especialista_por_defecto_no_hay_release_y_el_primero_que_llega_lo_es(app_client) -> None:
    """ADR-0192 §1-2: el build falla `sin_especialista` si la conexión solo tiene el Coordinador, y el
    primer especialista habilitado que se agrega queda por defecto aunque no se pida."""
    from test_instancias_por_conexion_api import _especialista, _instancias

    from core.agents.instancias import agregar_instancia
    from core.releases.build import ErrorDelBuild, construir
    from core.shared.tenant import tenant_scope

    tid, _cuenta, _phone, cid = await _con_agentes()
    await _sql(
        tid,
        "update agent_instances set deleted_at = now() where tenant_id = :t and connection_id = :c and role = 'specialist'",
        t=tid, c=cid,
    )
    async with tenant_scope(tid):
        with pytest.raises(ErrorDelBuild) as error:
            await construir(tid, cid, actor="test", fijadas={})
    assert error.value.codigo == "sin_especialista"

    ventas = await _especialista(app_client, await api_headers(app_client, tid), "ventas")
    async with tenant_scope(tid):
        await agregar_instancia(tid, cid, uuid.UUID(ventas))
    assert ("ventas", "specialist", True, True, 100) in await _instancias(tid, cid)
    async with tenant_scope(tid):
        resultado = await construir(tid, cid, actor="test", fijadas={})
    assert resultado.release_id

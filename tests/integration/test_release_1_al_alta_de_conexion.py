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

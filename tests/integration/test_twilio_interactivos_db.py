"""ADR-0186 (PR-7i) · botones y listas por Twilio contra Postgres real (migración 0124).

- La primera vez de una forma se crea el Content y se cachea; la segunda se reutiliza (un solo `POST /Content`).
- Otra forma (lista) crea su propio Content.
- Un Content borrado en la consola (404 al enviar) se olvida y se recrea una vez.
- Aislamiento: otro negocio no ve la caché; los CHECK rechazan formas y SIDs inválidos.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

XE = "XE" + "c" * 32
TO = "whatsapp:+573001234567"
BOTONES = {
    "type": "button",
    "body": "¿Te agendo?",
    "buttons": [{"id": "opt:si", "title": "Sí"}, {"id": "opt:no", "title": "No"}],
}
LISTA = {
    "type": "list",
    "body": "Elige",
    "button": "Ver",
    "rows": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}, {"id": "c", "title": "C"}],
}


def _alta(ac: str, e164: str) -> httpx.MockTransport:
    estado: dict[str, Any] = {"webhook": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/Accounts/{ac}.json"):
            return httpx.Response(200, json={"status": "active", "friendly_name": "C", "type": "Full"})
        if path == f"/v2/Channels/Senders/{XE}":
            if request.method == "POST":
                estado["webhook"] = json.loads(request.content)["webhook"]
            return httpx.Response(
                200,
                json={"sid": XE, "sender_id": f"whatsapp:{e164}", "status": "ONLINE", "webhook": estado["webhook"]},
            )
        return httpx.Response(404, json={"code": 20404})

    return httpx.MockTransport(handler)


class _Twilio:
    """Content API y Messages simulados: cuenta los `POST /Content` y puede contestar 404 a un ContentSid."""

    def __init__(self) -> None:
        self.creados: list[dict[str, Any]] = []
        self.enviados: list[dict[str, str]] = []
        self.borrados: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "content.twilio.com" and request.method == "POST":
                cuerpo = json.loads(request.content)
                sid = "HX" + uuid.uuid4().hex
                self.creados.append({"sid": sid, **cuerpo})
                return httpx.Response(201, json={"sid": sid, **cuerpo})
            if request.url.path.endswith("/Messages.json"):
                form = dict(httpx.QueryParams(request.content.decode()))
                self.enviados.append(form)
                if form.get("ContentSid") in self.borrados:
                    return httpx.Response(404, json={"code": 20404, "message": "not found"})
                return httpx.Response(201, json={"sid": "SM" + uuid.uuid4().hex})
            return httpx.Response(404, json={"code": 20404})

        return httpx.MockTransport(handler)


async def _negocio(prefijo: str) -> dict[str, Any]:
    from core.alta_negocio import onboard_tenant
    from core.gateway.twilio import alta, bandera
    from core.shared.tenant import tenant_scope

    slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Twilio {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    ac = "AC" + uuid.uuid4().hex
    e164 = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    async with tenant_scope(tid):
        await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
        hecha = await alta.conectar(tid, account_sid=ac, auth_token="tok", sender_sid=XE, transport=_alta(ac, e164))
    creds = {"account_sid": ac, "auth_token": "tok", "from": f"whatsapp:{e164}"}
    return {"tid": tid, "slug": slug, "connection_id": hecha.connection_id, "creds": creds}


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[dict[str, Any], dict[str, Any]]]:
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    a = await _negocio("tia")
    b = await _negocio("tib")
    yield a, b
    for n in (a, b):
        async with tenant_scope(n["tid"]):
            await deactivate_tenant(str(n["tid"]))
        await purge_tenant(str(n["tid"]), f"ELIMINAR {n['slug']}")


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            r = await session.execute(text(sql), params)
            return list(r.all()) if r.returns_rows else []


async def _enviar(n: dict[str, Any], twilio: _Twilio, interactivo: dict[str, Any]) -> str:
    from core.gateway.twilio import interactivos
    from core.shared.tenant import tenant_scope

    async with tenant_scope(n["tid"]):
        return await interactivos.enviar(
            n["tid"], n["connection_id"], n["creds"], TO, interactivo, transport=twilio.transport()
        )


async def test_misma_forma_reutiliza_el_content(negocios) -> None:
    a, _ = negocios
    twilio = _Twilio()
    await _enviar(a, twilio, BOTONES)
    await _enviar(a, twilio, {**BOTONES, "body": "Otro texto"})
    assert len(twilio.creados) == 1
    creado = twilio.creados[0]
    assert "twilio/quick-reply" in creado["types"]
    # Ejemplos neutros: el Content no guarda datos del primer mensaje.
    assert "¿Te agendo?" not in json.dumps(creado["variables"], ensure_ascii=False)
    assert [json.loads(f["ContentVariables"])["1"] for f in twilio.enviados] == ["¿Te agendo?", "Otro texto"]
    filas = await _sql(
        a["tid"],
        "select forma, content_sid from twilio_interactive_contents where deleted_at is null and channel_connection_id = :c",
        c=a["connection_id"],
    )
    assert filas == [("quick_reply:2", creado["sid"])]


async def test_otra_forma_crea_su_content(negocios) -> None:
    a, _ = negocios
    twilio = _Twilio()
    await _enviar(a, twilio, LISTA)
    assert len(twilio.creados) == 1 and "twilio/list-picker" in twilio.creados[0]["types"]
    formas = {
        f
        for (f,) in await _sql(
            a["tid"], "select forma from twilio_interactive_contents where deleted_at is null and tenant_id = :t", t=a["tid"]
        )
    }
    assert {"quick_reply:2", "list:3"} <= formas


async def test_content_borrado_en_la_consola_se_recrea(negocios) -> None:
    a, _ = negocios
    [(viejo,)] = await _sql(
        a["tid"],
        "select content_sid from twilio_interactive_contents where deleted_at is null and forma = 'quick_reply:2' "
        "and channel_connection_id = :c",
        c=a["connection_id"],
    )
    twilio = _Twilio()
    twilio.borrados.add(viejo)
    await _enviar(a, twilio, BOTONES)
    assert [f["ContentSid"] for f in twilio.enviados] == [viejo, twilio.creados[0]["sid"]]
    filas = await _sql(
        a["tid"],
        "select content_sid, deleted_at is not null from twilio_interactive_contents where forma = 'quick_reply:2' "
        "and channel_connection_id = :c order by created_at",
        c=a["connection_id"],
    )
    assert filas == [(viejo, True), (twilio.creados[0]["sid"], False)]


async def test_otro_negocio_no_ve_la_cache(negocios) -> None:
    a, b = negocios
    vistas = await _sql(b["tid"], "select count(*) from twilio_interactive_contents where tenant_id = :t", t=a["tid"])
    assert vistas == [(0,)]
    # B con la misma forma crea su propio Content: la caché de A no le sirve.
    twilio = _Twilio()
    await _enviar(b, twilio, BOTONES)
    assert len(twilio.creados) == 1


async def test_rls_rechaza_escribir_en_otro_negocio(negocios) -> None:
    a, b = negocios
    with pytest.raises((DBAPIError, IntegrityError)):
        await _sql(
            b["tid"],
            "insert into twilio_interactive_contents (id, tenant_id, channel_connection_id, forma, content_sid) "
            "values (:i, :t, :c, 'list:4', :s)",
            i=uuid.uuid4(),
            t=a["tid"],
            c=a["connection_id"],
            s="HX" + "d" * 32,
        )


@pytest.mark.parametrize(
    ("forma", "sid"), [("quick_reply:11", "HX" + "e" * 32), ("carrusel:1", "HX" + "e" * 32), ("list:2", "XX" + "e" * 32)]
)
async def test_check_de_forma_y_sid(negocios, forma, sid) -> None:
    a, _ = negocios
    with pytest.raises((DBAPIError, IntegrityError)):
        await _sql(
            a["tid"],
            "insert into twilio_interactive_contents (id, tenant_id, channel_connection_id, forma, content_sid) "
            "values (:i, :t, :c, :f, :s)",
            i=uuid.uuid4(),
            t=a["tid"],
            c=a["connection_id"],
            f=forma,
            s=sid,
        )

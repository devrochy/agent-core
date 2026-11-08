"""ADR-0186 (PR-4) · webhook de WhatsApp vía Twilio contra Postgres y Redis reales.

- Entrante firmado contra la URL registrada: se publica en el stream del core una sola vez (Twilio reintenta).
- Firma inválida, `AccountSid` ajeno o firma calculada sobre otra URL (la que vería el proxy): 403 y nada se publica.
- `public_id` desconocido: 404. Conexión pausada: 200 sin publicar.
- Status callbacks: el estado del mensaje avanza y nunca retrocede; `undelivered` queda `failed` con su código.
- La auditoría guarda campos de diagnóstico, nunca el texto del mensaje.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from agentcore_channel_twilio.signature import compute_signature
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AC = "AC" + "4" * 32
XE = "XE" + "5" * 32
TOKEN = "token-del-webhook"


def _numero() -> str:
    return f"+1555{uuid.uuid4().int % 10_000_000:07d}"


def _twilio(e164: str) -> httpx.MockTransport:
    estado: dict[str, Any] = {"webhook": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/Accounts/{AC}.json"):
            return httpx.Response(
                200, json={"status": "active", "friendly_name": "Cuenta", "type": "Full"}
            )
        if path == f"/v2/Channels/Senders/{XE}":
            if request.method == "POST":
                estado["webhook"] = json.loads(request.content)["webhook"]
            return httpx.Response(
                200,
                json={
                    "sid": XE,
                    "sender_id": f"whatsapp:{e164}",
                    "status": "ONLINE",
                    "webhook": estado["webhook"],
                },
            )
        return httpx.Response(404, json={"code": 20404})

    return httpx.MockTransport(handler)


@pytest.fixture(scope="module")
async def conexion() -> AsyncIterator[dict[str, Any]]:
    from core.alta_negocio import onboard_tenant
    from core.gateway.twilio import alta, bandera
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"twh-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"Twilio {slug}", slug=slug, packs=[], sembrar_ejemplos=False
    )
    e164 = _numero()
    async with tenant_scope(tid):
        await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
        hecha = await alta.conectar(
            tid, account_sid=AC, auth_token=TOKEN, sender_sid=XE, transport=_twilio(e164)
        )
    public_id = hecha.webhook_inbound_url.rstrip("/").split("/")[-2]
    yield {
        "tid": tid,
        "connection_id": hecha.connection_id,
        "e164": e164,
        "public_id": public_id,
        "inbound_url": hecha.webhook_inbound_url,
        "status_url": hecha.webhook_status_url,
    }
    async with tenant_scope(tid):
        await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if resultado.returns_rows else []


def _entrante(c: dict[str, Any], **extra: str) -> list[tuple[str, str]]:
    base = {
        "MessageSid": "SM" + uuid.uuid4().hex,
        "AccountSid": AC,
        "From": "whatsapp:+573001234567",
        "To": f"whatsapp:{c['e164']}",
        "Body": "Hola, quiero información confidencial",
        "ProfileName": "Ana",
        "NumMedia": "0",
    }
    return list({**base, **extra}.items())


async def _post(
    app_client,
    c: dict[str, Any],
    kind: str,
    params: list[tuple[str, str]],
    *,
    url: str | None = None,
    token: str = TOKEN,
    public_id: str | None = None,
):
    firma = compute_signature(token, url or c[f"{kind}_url"], params)
    return await app_client.post(
        f"/webhooks/twilio/{public_id or c['public_id']}/{kind}",
        content=urlencode(params),
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": firma},
    )


async def _encolados(sid: str) -> int:
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    filas = await get_redis().xrevrange(INBOUND_STREAM, count=500)
    return sum(1 for _id, campos in filas if campos.get("external_message_id") == sid)


async def test_entrante_se_publica_una_vez(conexion, app_client) -> None:
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    params = _entrante(conexion)
    sid = dict(params)["MessageSid"]
    r = await _post(app_client, conexion, "inbound", params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/xml") and "<Response/>" in r.text
    r = await _post(app_client, conexion, "inbound", params)  # reintento de Twilio
    assert r.status_code == 200
    assert await _encolados(sid) == 1

    [(_id, campos)] = [
        f
        for f in await get_redis().xrevrange(INBOUND_STREAM, count=500)
        if f[1]["external_message_id"] == sid
    ]
    assert campos["provider"] == "twilio" and campos["channel"] == "whatsapp"
    assert campos["customer_external_id"] == "whatsapp:+573001234567"
    assert campos["connection_external_id"] == conexion["e164"]
    assert campos["tenant_id"] == str(conexion["tid"])

    # La auditoría no guarda el texto del mensaje.
    [(payload,)] = await _sql(
        None,
        "select payload from webhook_events where meta_app_id = :a and external_id = :e",
        a=f"twilio:{AC}",
        e=sid,
    )
    assert "confidencial" not in json.dumps(payload) and payload["MessageSid"] == sid


@pytest.mark.parametrize("caso", ["token", "url_del_proxy", "account_ajeno"])
async def test_firma_invalida_es_403_y_no_publica(conexion, app_client, caso) -> None:
    if caso == "account_ajeno":
        params = _entrante(conexion, AccountSid="AC" + "9" * 32)
        r = await _post(app_client, conexion, "inbound", params)
    elif caso == "token":
        params = _entrante(conexion)
        r = await _post(app_client, conexion, "inbound", params, token="otro-token")
    else:
        params = _entrante(conexion)
        r = await _post(
            app_client,
            conexion,
            "inbound",
            params,
            url=f"http://backend:8000/webhooks/twilio/{conexion['public_id']}/inbound",
        )
    assert r.status_code == 403
    assert await _encolados(dict(params)["MessageSid"]) == 0


async def test_public_id_desconocido_es_404(conexion, app_client) -> None:
    r = await _post(
        app_client,
        conexion,
        "inbound",
        _entrante(conexion),
        public_id="desconocido" + uuid.uuid4().hex[:8],
    )
    assert r.status_code == 404


async def test_conexion_pausada_responde_200_sin_publicar(conexion, app_client) -> None:
    tid, cid = conexion["tid"], conexion["connection_id"]
    await _sql(tid, "update channel_connections set active = false where id = :c", c=cid)
    try:
        params = _entrante(conexion)
        r = await _post(app_client, conexion, "inbound", params)
        assert r.status_code == 200
        assert await _encolados(dict(params)["MessageSid"]) == 0
    finally:
        await _sql(tid, "update channel_connections set active = true where id = :c", c=cid)


async def test_status_avanza_y_no_retrocede(conexion, app_client) -> None:
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = conexion["tid"]
    sid = "SM" + uuid.uuid4().hex
    sid_fallido = "SM" + uuid.uuid4().hex
    async with tenant_scope(tid):
        async with get_session() as s:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{_numero()}", name="Ana"
            )
            s.add(cliente)
            await s.flush()
            conv = Conversation(
                tenant_id=tid,
                customer_id=cliente.id,
                channel="whatsapp",
                connection_id=conexion["connection_id"],
            )
            s.add(conv)
            await s.flush()
            for external in (sid, sid_fallido):
                s.add(
                    Message(
                        tenant_id=tid,
                        conversation_id=conv.id,
                        direction="outbound",
                        sender_type="agent",
                        content="hola",
                        status="sent",
                        external_message_id=external,
                    )
                )

    def _estado(s: str, status: str, **extra: str) -> list[tuple[str, str]]:
        return list({"MessageSid": s, "AccountSid": AC, "MessageStatus": status, **extra}.items())

    for status in ("delivered", "sent"):  # fuera de orden: `sent` llega después de `delivered`
        r = await _post(app_client, conexion, "status", _estado(sid, status))
        assert r.status_code == 200, r.text
    r = await _post(
        app_client, conexion, "status", _estado(sid_fallido, "undelivered", ErrorCode="63016")
    )
    assert r.status_code == 200

    filas = dict(
        (row[0], (row[1], row[2]))
        for row in await _sql(
            tid,
            "select external_message_id, status, error_code from messages where external_message_id in (:a, :b)",
            a=sid,
            b=sid_fallido,
        )
    )
    assert filas[sid] == ("delivered", None)
    assert filas[sid_fallido] == ("failed", 63016)


async def test_to_ajeno_no_se_publica(conexion, app_client) -> None:
    """Firmado con el token correcto pero con otro `To`: la conversación no puede nacer sin su conexión."""
    params = _entrante(conexion, To="whatsapp:+15559990000")
    r = await _post(app_client, conexion, "inbound", params)
    assert r.status_code == 200
    assert await _encolados(dict(params)["MessageSid"]) == 0

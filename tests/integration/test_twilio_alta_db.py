"""ADR-0186 (PR-3) · alta de WhatsApp vía Twilio contra Postgres real, con Twilio simulado (`httpx.MockTransport`).

- La bandera del negocio abre el tipo en el catálogo y el alta; apagada, el alta es 404. Solo el master la cambia.
- El alta guarda la conexión (credenciales cifradas) y su fila `twilio_connections` en una transacción, registra
  los webhooks en el sender y queda `connected`. Repetirla es idempotente (misma conexión, mismo `public_id`).
- Un número, un negocio: el mismo sender en otro negocio es 409 (índice único global de 0121).
- RLS: un negocio no ve la fila `twilio_connections` de otro; `twilio_route_connection` resuelve el negocio por
  `public_id` sin `tenant_scope`.
- `resolve_credentials` arma lo que espera el adaptador (`from` y `status_callback_url`).

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
from _api_helpers import api_headers
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AC = "AC" + "1" * 32
AC_OTRA = "AC" + "3" * 32
XE = "XE" + "2" * 32


def _numero() -> str:
    return f"+1555{uuid.uuid4().int % 10_000_000:07d}"


class TwilioFalso:
    """Una cuenta de Twilio con un sender ONLINE. Guarda los webhooks que se le escriben."""

    def __init__(
        self, e164: str, *, webhook: dict[str, str] | None = None, account_sid: str = AC
    ) -> None:
        self.e164 = e164
        self.account_sid = account_sid
        self.webhook: dict[str, Any] = dict(webhook or {})

    def _sender(self) -> dict[str, Any]:
        return {
            "sid": XE,
            "sender_id": f"whatsapp:{self.e164}",
            "status": "ONLINE",
            "webhook": self.webhook,
            "profile": {"name": "SolversHub"},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/Accounts/{self.account_sid}.json"):
            return httpx.Response(
                200, json={"status": "active", "friendly_name": "Cuenta", "type": "Full"}
            )
        if path == "/v2/Channels/Senders":
            return httpx.Response(200, json={"senders": [self._sender()], "meta": {}})
        if path == f"/v2/Channels/Senders/{XE}":
            if request.method == "POST":
                self.webhook = json.loads(request.content)["webhook"]
            return httpx.Response(200, json=self._sender())
        return httpx.Response(404, json={"code": 20404})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo in ("twa", "twb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(
            name=f"Twilio {slug}", slug=slug, packs=[], sembrar_ejemplos=False
        )
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        async with tenant_scope(tid):
            await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _bandera(tid: uuid.UUID, activa: bool) -> None:
    from core.gateway.twilio import bandera
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await bandera.fijar(tid, "whatsapp_twilio", activa, actor="test")


async def _conectar(tid: uuid.UUID, twilio: TwilioFalso, **extra: Any):
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await alta.conectar(
            tid,
            account_sid=twilio.account_sid,
            auth_token="token-secreto",
            sender_sid=XE,
            transport=twilio.transport,
            **extra,
        )


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list((await session.execute(text(sql), params)).all())


async def test_alta_guarda_registra_webhooks_y_es_idempotente(negocios) -> None:
    tid, _ = negocios
    twilio = TwilioFalso(_numero())
    primera = await _conectar(tid, twilio, label="Comercial")

    assert primera.created is True
    assert primera.status == "connected"
    assert primera.sender_e164 == twilio.e164
    assert twilio.webhook["callback_url"] == primera.webhook_inbound_url
    assert twilio.webhook["status_callback_url"] == primera.webhook_status_url
    assert (
        primera.webhook_inbound_url.endswith("/inbound")
        and "/webhooks/twilio/" in primera.webhook_inbound_url
    )

    [(cifradas, external_id, provider, type_id)] = await _sql(
        tid,
        "select credentials_encrypted, external_id, provider, type_id from channel_connections where id = :c",
        c=primera.connection_id,
    )
    assert (external_id, provider, type_id) == (twilio.e164, "twilio", "whatsapp_twilio")
    assert "token-secreto" not in (cifradas or "")

    segunda = await _conectar(tid, twilio)
    assert segunda.created is False
    assert segunda.connection_id == primera.connection_id
    assert segunda.webhook_inbound_url == primera.webhook_inbound_url
    filas = await _sql(
        tid,
        "select count(*) from twilio_connections where channel_connection_id = :c",
        c=primera.connection_id,
    )
    assert filas == [(1,)]


async def test_webhook_ajeno_exige_confirmar_y_se_guarda(negocios) -> None:
    tid, _ = negocios
    twilio = TwilioFalso(_numero(), webhook={"callback_url": "https://otra.app/in"})
    from core.gateway.twilio.alta import AltaError

    with pytest.raises(AltaError) as exc:
        await _conectar(tid, twilio)
    assert exc.value.code == "webhook_overwrite_not_confirmed"
    assert (
        await _sql(tid, "select 1 from channel_connections where external_id = :e", e=twilio.e164)
        == []
    )

    hecha = await _conectar(tid, twilio, confirm_overwrite_webhook=True)
    assert hecha.tenia_webhook_previo is True
    [(previo,)] = await _sql(
        tid,
        "select previous_webhook from twilio_connections where channel_connection_id = :c",
        c=hecha.connection_id,
    )
    assert previo == {"callback_url": "https://otra.app/in"}


async def test_un_numero_un_negocio_y_rls(negocios) -> None:
    tid_a, tid_b = negocios
    twilio = TwilioFalso(_numero())
    hecha = await _conectar(tid_a, twilio)
    from core.gateway.twilio.alta import AltaError

    # La misma cuenta en otro negocio, aunque sea otro número: su Auth Token firma los webhooks de A.
    with pytest.raises(AltaError) as exc:
        await _conectar(tid_b, TwilioFalso(_numero()))
    assert (exc.value.http_status, exc.value.code) == (409, "cuenta_en_otro_negocio")
    # Otra cuenta, el mismo número: un número, un negocio.
    with pytest.raises(AltaError) as exc:
        await _conectar(tid_b, TwilioFalso(twilio.e164, account_sid=AC_OTRA))
    assert (exc.value.http_status, exc.value.code) == (409, "sender_ya_conectado")

    # B no ve la fila de A, ni con el id exacto.
    assert (
        await _sql(
            tid_b,
            "select 1 from twilio_connections where channel_connection_id = :c",
            c=hecha.connection_id,
        )
        == []
    )
    [(public_id,)] = await _sql(
        tid_a,
        "select public_id from twilio_connections where channel_connection_id = :c",
        c=hecha.connection_id,
    )
    # El webhook resuelve el negocio por `public_id`, sin `tenant_scope`.
    ruta = await _sql(
        None,
        "select tenant_id, channel_connection_id from twilio_route_connection(:p)",
        p=public_id,
    )
    assert ruta == [(tid_a, hecha.connection_id)]
    assert (
        await _sql(
            None, "select * from twilio_route_connection(:p)", p="no-existe-" + uuid.uuid4().hex
        )
        == []
    )


async def test_conectar_fuera_del_scope_no_escribe(negocios) -> None:
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    tid_a, tid_b = negocios
    twilio = TwilioFalso(_numero())
    async with tenant_scope(tid_b):
        with pytest.raises(PermissionError):
            await alta.conectar(
                tid_a, account_sid=AC, auth_token="x", sender_sid=XE, transport=twilio.transport
            )


async def test_resolve_credentials_arma_las_del_adaptador(negocios) -> None:
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid, otro = negocios
    twilio = TwilioFalso(_numero())
    hecha = await _conectar(tid, twilio)
    async with tenant_scope(tid):
        provider, creds = await resolve_credentials(hecha.connection_id, expected_tenant_id=tid)
    assert provider == "twilio"
    assert creds == {
        "account_sid": AC,
        "auth_token": "token-secreto",
        "from": f"whatsapp:{twilio.e164}",
        "status_callback_url": hecha.webhook_status_url,
    }
    # Con el negocio equivocado, nada (no cae a la primaria ni al `.env`).
    async with tenant_scope(otro):
        provider, creds = await resolve_credentials(hecha.connection_id, expected_tenant_id=otro)
    assert creds.get("auth_token") is None


async def test_api_bandera_catalogo_y_alta(negocios, app_client, monkeypatch) -> None:
    from agentcore_channel_twilio.client import TwilioClient

    from core.gateway.twilio import alta

    tid, _ = negocios
    twilio = TwilioFalso(_numero())
    monkeypatch.setattr(
        alta,
        "_cliente",
        lambda sid, token, transport: TwilioClient(sid, token, transport=twilio.transport),
    )
    h = await api_headers(app_client, tid)
    cuerpo = {"account_sid": AC, "auth_token": "token-secreto"}

    await _bandera(tid, False)
    r = await app_client.post("/api/v1/admin/connections/twilio/discover", headers=h, json=cuerpo)
    assert r.status_code == 404, r.text
    catalogo = (await app_client.get("/api/v1/admin/connections/catalog", headers=h)).json()
    tipo = next(
        i for c in catalogo["categories"] for i in c["items"] if i["id"] == "whatsapp_twilio"
    )
    assert tipo["available"] is False

    r = await app_client.put(
        f"/api/v1/admin/platform/tenants/{tid}/canales/whatsapp_twilio",
        headers=h,
        json={"habilitado": True},
    )
    assert r.status_code == 200 and r.json() == {"canal": "whatsapp_twilio", "habilitado": True}, (
        r.text
    )
    r = await app_client.put(
        f"/api/v1/admin/platform/tenants/{tid}/canales/telegram",
        headers=h,
        json={"habilitado": True},
    )
    assert r.status_code == 404
    catalogo = (await app_client.get("/api/v1/admin/connections/catalog", headers=h)).json()
    tipo = next(
        i for c in catalogo["categories"] for i in c["items"] if i["id"] == "whatsapp_twilio"
    )
    assert tipo["available"] is True and tipo["setup_kind"] == "wizard"

    r = await app_client.post("/api/v1/admin/connections/twilio/discover", headers=h, json=cuerpo)
    assert r.status_code == 200, r.text
    assert [s["e164"] for s in r.json()["senders"]] == [twilio.e164]
    assert "token-secreto" not in r.text

    r = await app_client.post(
        "/api/v1/admin/connections/twilio", headers=h, json={**cuerpo, "sender_sid": XE}
    )
    assert r.status_code == 201, r.text
    creada = r.json()
    assert creada["status"] == "connected" and "token-secreto" not in r.text
    r = await app_client.post(
        "/api/v1/admin/connections/twilio", headers=h, json={**cuerpo, "sender_sid": XE}
    )
    assert r.status_code == 200 and r.json()["connection_id"] == creada["connection_id"]
    r = await app_client.get(
        f"/api/v1/admin/connections/twilio/{creada['connection_id']}", headers=h
    )
    assert r.status_code == 200 and r.json()["sender_e164"] == twilio.e164
    # El formulario manual genérico no sirve para Twilio: el alta guiada es la única puerta.
    r = await app_client.get("/api/v1/admin/connections/manual/whatsapp_twilio", headers=h)
    assert r.status_code == 404
    await _bandera(tid, False)

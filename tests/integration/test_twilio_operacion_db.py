"""ADR-0186 (PR-5) · salud, operación y costos de WhatsApp vía Twilio contra Postgres real, con Twilio simulado.

- Salud: `connected` / `degraded` (sender no online, webhooks cambiados) / `revoked` (token rechazado); una caída
  transitoria no cambia el estado. El enumerador del cron ve las conexiones de todos los negocios.
- Re-registrar webhooks, mensaje de prueba (ventana de 24 h, topes), rotar credenciales, desconectar (libera el
  número y restaura los webhooks anteriores) y el borrado genérico, que también se lleva la fila de Twilio.
- Costos: un costo por mensaje, en positivo y con su moneda; sin precio todavía, no se guarda nada; el webhook de
  estados final encola la tarea.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, urlencode

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

XE = "XE" + "6" * 32


def _ac() -> str:
    return "AC" + uuid.uuid4().hex


def _numero() -> str:
    return f"+1555{uuid.uuid4().int % 10_000_000:07d}"


class Twilio:
    """Cuenta de Twilio simulada: cuenta, sender, envío de mensajes y precio del recurso Message."""

    def __init__(self, ac: str, e164: str, *, webhook: dict[str, str] | None = None) -> None:
        self.ac = ac
        self.e164 = e164
        self.cuenta = "active"
        self.sender_status = "ONLINE"
        self.webhook: dict[str, Any] = dict(webhook or {})
        self.token_valido = "token-1"
        self.error_envio: dict[str, Any] | None = None
        self.caido = False
        self.precio: str | None = "-0.00500"
        self.enviados: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.caido:
            return httpx.Response(503)
        auth = request.headers.get("authorization", "")
        import base64

        _, token = base64.b64decode(auth.split()[1]).decode().split(":", 1)
        if token != self.token_valido:
            return httpx.Response(401, json={"code": 20003, "message": "denied"})
        path = request.url.path
        if path.endswith(f"/Accounts/{self.ac}.json"):
            return httpx.Response(
                200, json={"status": self.cuenta, "friendly_name": "Cuenta", "type": "Full"}
            )
        if path == f"/v2/Channels/Senders/{XE}":
            if request.method == "POST":
                self.webhook = json.loads(request.content)["webhook"]
            return httpx.Response(
                200,
                json={
                    "sid": XE,
                    "sender_id": f"whatsapp:{self.e164}",
                    "status": self.sender_status,
                    "webhook": self.webhook,
                },
            )
        if path.endswith("/Messages.json") and request.method == "POST":
            if self.error_envio:
                return httpx.Response(400, json=self.error_envio)
            self.enviados.append(dict(parse_qsl(request.content.decode())))
            return httpx.Response(201, json={"sid": "SM" + uuid.uuid4().hex, "status": "queued"})
        if "/Messages/" in path and request.method == "GET":
            sid = path.rsplit("/", 1)[-1].removesuffix(".json")
            return httpx.Response(
                200,
                json={"sid": sid, "status": "delivered", "price": self.precio, "price_unit": "USD"},
            )
        return httpx.Response(404, json={"code": 20404})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.gateway.twilio import bandera
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo in ("two", "twp"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(
            name=f"Twilio {slug}", slug=slug, packs=[], sembrar_ejemplos=False
        )
        async with tenant_scope(tid):
            await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        async with tenant_scope(tid):
            await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


@pytest.fixture
def twilio_simulado(monkeypatch) -> list[Twilio]:
    """Cada `TwilioClient` del core habla con el último `Twilio` creado en el test."""
    from agentcore_channel_twilio import client as client_mod

    cuentas: list[Twilio] = []
    original = client_mod.TwilioClient.__init__

    def _init(self, account_sid, auth_token, *, transport=None):
        original(self, account_sid, auth_token, transport=transport or cuentas[-1].transport)

    monkeypatch.setattr(client_mod.TwilioClient, "__init__", _init)
    return cuentas


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if resultado.returns_rows else []


async def _conectar(tid: uuid.UUID, cuentas: list[Twilio], **kw: Any):
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    tw = Twilio(_ac(), _numero(), **kw)
    cuentas.append(tw)
    async with tenant_scope(tid):
        hecha = await alta.conectar(
            tid,
            account_sid=tw.ac,
            auth_token=tw.token_valido,
            sender_sid=XE,
            confirm_overwrite_webhook=True,
        )
    return tw, hecha


async def _en(tid: uuid.UUID, coro_fn, *args, **kwargs):
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await coro_fn(*args, **kwargs)


async def _mensaje(tid: uuid.UUID, connection_id: uuid.UUID, sid: str) -> None:
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

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
                connection_id=connection_id,
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    tenant_id=tid,
                    conversation_id=conv.id,
                    direction="outbound",
                    sender_type="agent",
                    content="hola",
                    status="delivered",
                    external_message_id=sid,
                )
            )


async def test_salud_por_estado(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import operacion

    tid, _ = negocios
    tw, hecha = await _conectar(tid, twilio_simulado)
    cid = hecha.connection_id
    assert (await _en(tid, operacion.revisar, tid, cid)).status == "connected"

    tw.sender_status = "OFFLINE"
    assert (await _en(tid, operacion.revisar, tid, cid)).status == "degraded"
    tw.sender_status = "ONLINE"
    tw.webhook = {"callback_url": "https://otra.app/in"}
    assert (await _en(tid, operacion.revisar, tid, cid)).status == "degraded"
    assert (await _en(tid, operacion.reregistrar_webhooks, tid, cid)).status == "connected"
    assert tw.webhook["callback_url"].endswith("/inbound#rc=3&rp=5xx,ct")

    tw.caido = True  # Twilio caído: no cambia el estado
    assert (await _en(tid, operacion.revisar, tid, cid)).status == "connected"
    tw.caido = False
    tw.token_valido = "token-rotado-en-la-consola"
    revisada = await _en(tid, operacion.revisar, tid, cid)
    assert (revisada.status, revisada.last_error_code) == ("revoked", 20003)

    # Rotar credenciales lo vuelve a dejar conectado, con el token nuevo para enviar.
    rotada = await _en(
        tid, operacion.rotar_credenciales, tid, cid, account_sid=tw.ac, auth_token=tw.token_valido
    )
    assert rotada.status == "connected"
    from core.gateway.sender import resolve_credentials

    _, creds = await _en(tid, resolve_credentials, cid, expected_tenant_id=tid)
    assert creds["auth_token"] == "token-rotado-en-la-consola"


async def test_el_cron_ve_todos_los_negocios(negocios, twilio_simulado) -> None:
    tid_a, tid_b = negocios
    _, a = await _conectar(tid_a, twilio_simulado)
    _, b = await _conectar(tid_b, twilio_simulado)
    filas = {
        (r[0], r[1])
        for r in await _sql(None, "select id, tenant_id from admin_twilio_connection_ids()")
    }
    assert {(a.connection_id, tid_a), (b.connection_id, tid_b)} <= filas
    # Una pausada no se revisa.
    await _sql(
        tid_b, "update channel_connections set active = false where id = :c", c=b.connection_id
    )
    filas = {r[0] for r in await _sql(None, "select id from admin_twilio_connection_ids()")}
    assert b.connection_id not in filas


async def test_mensaje_de_prueba(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError
    from core.memory.working import get_redis

    tid, _ = negocios
    tw, hecha = await _conectar(tid, twilio_simulado)
    sid = await _en(tid, operacion.enviar_prueba, tid, hecha.connection_id, "+573001234567")
    assert sid.startswith("SM")
    [enviado] = tw.enviados
    assert (enviado["To"], enviado["From"]) == ("whatsapp:+573001234567", f"whatsapp:{tw.e164}")
    assert enviado["StatusCallback"] == hecha.webhook_status_url + "#rc=3&rp=5xx,ct"

    with pytest.raises(AltaError) as exc:  # una cada 30 s
        await _en(tid, operacion.enviar_prueba, tid, hecha.connection_id, "+573001234567")
    assert exc.value.code == "too_many_tests"

    await get_redis().delete(f"twilio_prueba:{tid}:{hecha.connection_id}")
    tw.error_envio = {"code": 63016, "message": "Outside window"}
    with pytest.raises(AltaError) as exc:
        await _en(tid, operacion.enviar_prueba, tid, hecha.connection_id, "+573001234567")
    assert (exc.value.http_status, exc.value.code) == (409, "fuera_de_ventana")


async def test_desconectar_libera_el_numero_y_restaura(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import operacion

    tid_a, tid_b = negocios
    tw, hecha = await _conectar(
        tid_a, twilio_simulado, webhook={"callback_url": "https://otra.app/in"}
    )
    restaurados = await _en(
        tid_a, operacion.desconectar, tid_a, hecha.connection_id, restaurar_webhooks=True
    )
    assert restaurados is True
    assert (
        tw.webhook["callback_url"] == "https://otra.app/in"
        and tw.webhook["status_callback_url"] == ""
    )
    [(cc_borrada, tc_borrada)] = await _sql(
        tid_a,
        "select cc.deleted_at is not null, tc.deleted_at is not null from channel_connections cc "
        "join twilio_connections tc on tc.channel_connection_id = cc.id where cc.id = :c",
        c=hecha.connection_id,
    )
    assert cc_borrada and tc_borrada
    # El número queda libre: otro negocio lo puede conectar (con su propia cuenta).
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    otra = Twilio(_ac(), tw.e164)
    twilio_simulado.append(otra)
    async with tenant_scope(tid_b):
        hecha_b = await alta.conectar(
            tid_b, account_sid=otra.ac, auth_token=otra.token_valido, sender_sid=XE
        )
    assert hecha_b.sender_e164 == tw.e164


async def test_borrado_generico_se_lleva_la_fila_de_twilio(negocios, twilio_simulado) -> None:
    from core.gateway.connections import delete_connection

    tid, _ = negocios
    _, hecha = await _conectar(tid, twilio_simulado)
    await _en(tid, delete_connection, hecha.connection_id)
    [(borrada,)] = await _sql(
        tid,
        "select deleted_at is not null from twilio_connections where channel_connection_id = :c",
        c=hecha.connection_id,
    )
    assert borrada is True


async def test_costo_por_mensaje(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import costos

    tid, _ = negocios
    tw, hecha = await _conectar(tid, twilio_simulado)
    sid = "SM" + uuid.uuid4().hex
    # Sin el mensaje en el negocio no se guarda nada (como Meta).
    assert await _en(tid, costos.registrar_costo, tid, hecha.connection_id, sid) == "sin_mensaje"
    await _mensaje(tid, hecha.connection_id, sid)
    tw.precio = None
    assert await _en(tid, costos.registrar_costo, tid, hecha.connection_id, sid) == "sin_precio"
    tw.precio = "-0.00500"
    assert await _en(tid, costos.registrar_costo, tid, hecha.connection_id, sid) == "guardado"
    assert await _en(tid, costos.registrar_costo, tid, hecha.connection_id, sid) == "ya_estaba"
    [(provider, price, unidad, billable)] = await _sql(
        tid,
        "select provider, price, price_unit, billable from message_costs where external_message_id = :s",
        s=sid,
    )
    assert (provider, str(price), unidad, billable) == ("twilio", "0.005000", "usd", None)


async def test_el_estado_final_encola_el_costo(
    negocios, twilio_simulado, app_client, monkeypatch
) -> None:
    tid, _ = negocios
    tw, hecha = await _conectar(tid, twilio_simulado)
    encolados: list[tuple[Any, ...]] = []
    monkeypatch.setattr("core.tasks.twilio_costos.encolar_costo", lambda *a: encolados.append(a))
    public_id = hecha.webhook_status_url.rstrip("/").split("/")[-2]
    for status in ("sent", "delivered"):
        sid = "SM" + uuid.uuid4().hex
        params = [("MessageSid", sid), ("AccountSid", tw.ac), ("MessageStatus", status)]
        r = await app_client.post(
            f"/webhooks/twilio/{public_id}/status",
            content=urlencode(params),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": compute_signature(
                    tw.token_valido, hecha.webhook_status_url, params
                ),
            },
        )
        assert r.status_code == 200, r.text
    assert [(e[0], e[1]) for e in encolados] == [
        (tid, hecha.connection_id)
    ]  # solo el final (`delivered`)


async def test_rotar_una_pausada_no_la_reactiva(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    tw, hecha = await _conectar(tid, twilio_simulado)
    await _sql(
        tid, "update channel_connections set active = false where id = :c", c=hecha.connection_id
    )
    with pytest.raises(AltaError) as exc:
        await _en(
            tid,
            operacion.rotar_credenciales,
            tid,
            hecha.connection_id,
            account_sid=tw.ac,
            auth_token=tw.token_valido,
        )
    assert exc.value.code == "conexion_pausada"
    assert await _sql(
        tid, "select active from channel_connections where id = :c", c=hecha.connection_id
    ) == [(False,)]


async def test_el_webhook_original_no_se_pisa(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    tw, hecha = await _conectar(
        tid, twilio_simulado, webhook={"callback_url": "https://original.app/in"}
    )
    tw.webhook = {
        "callback_url": "https://alguien-lo-cambio.app/in"
    }  # cambiado en la consola de Twilio
    async with tenant_scope(tid):
        await alta.conectar(
            tid,
            account_sid=tw.ac,
            auth_token=tw.token_valido,
            sender_sid=XE,
            confirm_overwrite_webhook=True,
        )
    [(previo,)] = await _sql(
        tid,
        "select previous_webhook from twilio_connections where channel_connection_id = :c",
        c=hecha.connection_id,
    )
    assert previo == {"callback_url": "https://original.app/in"}


async def test_costos_piden_la_seccion_costos(negocios, twilio_simulado, app_client) -> None:
    from test_opciones_api import _headers

    tid, _ = negocios
    _, hecha = await _conectar(tid, twilio_simulado)
    url = f"/api/v1/admin/connections/twilio/{hecha.connection_id}/costs"
    sin = await _headers(
        app_client, tid, rol="colaborador", secciones={"connections": "read", "costs": "none"}
    )
    assert (await app_client.get(url, headers=sin)).status_code == 403
    con = await _headers(
        app_client, tid, rol="colaborador", secciones={"connections": "read", "costs": "read"}
    )
    r = await app_client.get(url, headers=con)
    assert r.status_code == 200 and r.json()["dias"] == 30, r.text


async def test_desconectar_si_no_se_restaura(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    tw, hecha = await _conectar(
        tid, twilio_simulado, webhook={"callback_url": "https://otra.app/in"}
    )
    tw.caido = True
    with pytest.raises(AltaError) as exc:
        await _en(tid, operacion.desconectar, tid, hecha.connection_id, restaurar_webhooks=True)
    assert exc.value.code == "webhooks_no_restaurados"
    assert await _sql(
        tid,
        "select deleted_at is null from channel_connections where id = :c",
        c=hecha.connection_id,
    ) == [(True,)]
    restaurados = await _en(
        tid,
        operacion.desconectar,
        tid,
        hecha.connection_id,
        restaurar_webhooks=True,
        continuar_si_falla=True,
    )
    assert restaurados is False
    assert await _sql(
        tid,
        "select deleted_at is null from channel_connections where id = :c",
        c=hecha.connection_id,
    ) == [(False,)]


async def test_desconectar_no_pisa_una_integracion_nueva(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import operacion

    tid, _ = negocios
    tw, hecha = await _conectar(
        tid, twilio_simulado, webhook={"callback_url": "https://original.app/in"}
    )
    tw.webhook = {"callback_url": "https://otra-integracion.app/in"}  # el dueño ya lo movió
    assert (
        await _en(tid, operacion.desconectar, tid, hecha.connection_id, restaurar_webhooks=True)
        is False
    )
    assert tw.webhook == {"callback_url": "https://otra-integracion.app/in"}


async def test_rotar_con_webhook_ajeno_pide_confirmar(negocios, twilio_simulado) -> None:
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    tw, hecha = await _conectar(tid, twilio_simulado)
    tw.webhook = {"callback_url": "https://otra.app/in"}
    with pytest.raises(AltaError) as exc:
        await _en(
            tid,
            operacion.rotar_credenciales,
            tid,
            hecha.connection_id,
            account_sid=tw.ac,
            auth_token=tw.token_valido,
        )
    assert exc.value.code == "webhook_overwrite_not_confirmed"
    ok = await _en(
        tid,
        operacion.rotar_credenciales,
        tid,
        hecha.connection_id,
        account_sid=tw.ac,
        auth_token=tw.token_valido,
        confirm_overwrite_webhook=True,
    )
    assert ok.status == "connected"


async def test_entre_negocios(negocios, twilio_simulado, app_client) -> None:
    from _api_helpers import api_headers

    from core.gateway.twilio import costos, operacion
    from core.gateway.twilio.alta import AltaError

    tid_a, tid_b = negocios
    tw_a, a = await _conectar(tid_a, twilio_simulado)
    tw_b, b = await _conectar(tid_b, twilio_simulado)
    # (a) el mismo SID en dos negocios: cada uno guarda el suyo.
    sid = "SM" + uuid.uuid4().hex
    await _mensaje(tid_a, a.connection_id, sid)
    await _mensaje(tid_b, b.connection_id, sid)
    twilio_simulado.append(tw_a)
    assert await _en(tid_a, costos.registrar_costo, tid_a, a.connection_id, sid) == "guardado"
    twilio_simulado.append(tw_b)
    assert await _en(tid_b, costos.registrar_costo, tid_b, b.connection_id, sid) == "guardado"
    # (b) el connection_id de A desde B da 404.
    h_b = await api_headers(app_client, tid_b)
    base = f"/api/v1/admin/connections/twilio/{a.connection_id}"
    for metodo, ruta, cuerpo in [
        ("post", "/refresh", None),
        ("post", "/test-message", {"to": "+573001234567"}),
        (
            "put",
            "/credentials",
            {"account_sid": tw_a.ac, "auth_token": "x", "password": "admin123"},
        ),
        ("post", "/disconnect", {"password": "admin123"}),
        ("get", "/costs", None),
    ]:
        r = await getattr(app_client, metodo)(
            base + ruta, headers=h_b, **({"json": cuerpo} if cuerpo else {})
        )
        assert r.status_code == 404, (ruta, r.text)
    # (c) rotar B hacia la cuenta de A: «una cuenta, un negocio».
    twilio_simulado.append(tw_a)
    with pytest.raises(AltaError) as exc:
        await _en(
            tid_b,
            operacion.rotar_credenciales,
            tid_b,
            b.connection_id,
            account_sid=tw_a.ac,
            auth_token=tw_a.token_valido,
        )
    assert exc.value.code in ("cuenta_en_otro_negocio", "sender_not_found")

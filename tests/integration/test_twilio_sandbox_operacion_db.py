"""HU-CONN-20 (adenda del ADR-0186) · operación del Sandbox de Twilio contra Postgres y Redis reales.

- Webhook: el primer entrante o estado con firma válida confirma las URL pegadas en la consola (una sola vez, también
  en paralelo) y pasa la conexión de `pending` a `connected`. Firma inválida o `AccountSid` ajeno: 403 y no confirma.
  Con el gate apagado: 200 sin procesar.
- Salud: solo la cuenta y la confirmación; nunca la Senders API.
- Re-registrar webhooks: 409 `sandbox_webhook_manual` con las URL. Desconectar: no restaura nada.
- Mensaje de prueba: 63015 → 422 `participante_no_unido` con el `join <código>`.
- Rotar: revalida la cuenta y conserva la conexión; otra cuenta es 422.
- Plantillas: crear, enviar a aprobación y borrar son 409 `no_aplica_sandbox`.
- Con el gate apagado el envío falla cerrado.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from _api_helpers import api_headers
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

SANDBOX = "+14155238886"
TOKEN = "token-del-sandbox"


def _ac() -> str:
    return "AC" + uuid.uuid4().hex


class Twilio:
    """Cuenta con su sandbox: responde la cuenta y Messages. Anota cualquier llamada a la Senders API."""

    def __init__(self, ac: str) -> None:
        self.ac = ac
        self.status = "active"
        self.error_envio: int | None = None
        self.senders: list[str] = []
        self.enviados: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/Channels/Senders" in path:
            self.senders.append(path)
            return httpx.Response(404, json={"code": 20404})
        if path.endswith(f"/Accounts/{self.ac}.json"):
            return httpx.Response(
                200, json={"status": self.status, "friendly_name": "C", "type": "Trial"}
            )
        if path.endswith(f"/Accounts/{self.ac}/Messages.json") and request.method == "POST":
            if self.error_envio:
                return httpx.Response(
                    400, json={"code": self.error_envio, "message": "x", "status": 400}
                )
            from urllib.parse import parse_qsl

            self.enviados.append(dict(parse_qsl(request.content.decode())))
            return httpx.Response(201, json={"sid": "SM" + uuid.uuid4().hex, "status": "queued"})
        return httpx.Response(404, json={"code": 20404})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(scope="module")
async def negocio() -> AsyncIterator[uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.gateway.twilio import bandera
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"tso-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"Sandbox {slug}", slug=slug, packs=[], sembrar_ejemplos=False
    )
    async with tenant_scope(tid):
        await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
    yield tid
    async with tenant_scope(tid):
        await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


@pytest.fixture
def gate(monkeypatch):
    from core.config import get_settings

    def fijar(valor: bool) -> None:
        monkeypatch.setattr(get_settings(), "twilio_sandbox_permitido", valor)

    fijar(True)
    return fijar


@pytest.fixture
def twilio(monkeypatch) -> list[Twilio]:
    """Cada `TwilioClient` del core habla con el último `Twilio` creado en el test."""
    from agentcore_channel_twilio import client as client_mod

    cuentas: list[Twilio] = []
    original = client_mod.TwilioClient.__init__

    def _init(self, account_sid, auth_token, *, transport=None):
        original(self, account_sid, auth_token, transport=transport or cuentas[-1].transport)

    monkeypatch.setattr(client_mod.TwilioClient, "__init__", _init)
    return cuentas


async def _en(tid: uuid.UUID, coro_fn, *args, **kwargs):
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await coro_fn(*args, **kwargs)


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if resultado.returns_rows else []


async def _sandbox(tid: uuid.UUID, cuentas: list[Twilio], **kw: Any):
    from core.gateway.twilio import alta

    tw = Twilio(_ac())
    cuentas.append(tw)
    hecha = await _en(
        tid,
        alta.conectar_sandbox,
        tid,
        account_sid=tw.ac,
        auth_token=TOKEN,
        join_code="cloud-forgotten",
        **kw,
    )
    return tw, hecha


async def _post(app_client, hecha, tw: Twilio, kind: str, *, token: str = TOKEN, **extra: str):
    if kind == "inbound":
        base = {
            "MessageSid": "SM" + uuid.uuid4().hex,
            "AccountSid": tw.ac,
            "From": "whatsapp:+573001234567",
            "To": f"whatsapp:{SANDBOX}",
            "Body": "hola",
            "NumMedia": "0",
        }
        url = hecha.webhook_inbound_url
    else:
        base = {"MessageSid": "SM" + uuid.uuid4().hex, "AccountSid": tw.ac, "MessageStatus": "sent"}
        url = hecha.webhook_status_url
    params = list({**base, **extra}.items())
    public_id = url.rstrip("/").split("/")[-2]
    return await app_client.post(
        f"/webhooks/twilio/{public_id}/{kind}",
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": compute_signature(token, url, params),
        },
    ), dict(params)["MessageSid"]


async def _estado(tid: uuid.UUID, connection_id: uuid.UUID) -> tuple[str, Any]:
    [(status, confirmado)] = await _sql(
        tid,
        "select status, webhook_confirmado_at from twilio_connections where channel_connection_id = :c "
        "and deleted_at is null",
        c=connection_id,
    )
    return status, confirmado


async def _encolados(sid: str) -> int:
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    filas = await get_redis().xrevrange(INBOUND_STREAM, count=500)
    return sum(1 for _id, campos in filas if campos.get("external_message_id") == sid)


async def test_el_primer_entrante_firmado_confirma_una_sola_vez(
    negocio, app_client, gate, twilio
) -> None:
    tw, hecha = await _sandbox(negocio, twilio)
    assert await _estado(negocio, hecha.connection_id) == ("pending", None)

    # Firma con otro token o cuenta ajena: 403 y no confirma.
    r, _ = await _post(app_client, hecha, tw, "inbound", token="otro")
    assert r.status_code == 403
    r, _ = await _post(app_client, hecha, tw, "inbound", AccountSid=_ac())
    assert r.status_code == 403
    assert await _estado(negocio, hecha.connection_id) == ("pending", None)

    respuestas = await asyncio.gather(*(_post(app_client, hecha, tw, "inbound") for _ in range(3)))
    assert all(r.status_code == 200 for r, _ in respuestas)
    status, confirmado = await _estado(negocio, hecha.connection_id)
    assert status == "connected" and confirmado is not None
    assert all([await _encolados(sid) == 1 for _, sid in respuestas])

    r, _ = await _post(app_client, hecha, tw, "inbound")
    assert r.status_code == 200
    assert await _estado(negocio, hecha.connection_id) == ("connected", confirmado)


async def test_un_estado_firmado_tambien_confirma(negocio, app_client, gate, twilio) -> None:
    tw, hecha = await _sandbox(negocio, twilio)
    r, _ = await _post(app_client, hecha, tw, "status")
    assert r.status_code == 200, r.text
    status, confirmado = await _estado(negocio, hecha.connection_id)
    assert status == "connected" and confirmado is not None


async def test_con_el_gate_apagado_el_webhook_no_procesa_y_el_envio_cierra(
    negocio, app_client, gate, twilio
) -> None:
    from core.gateway.sender import resolve_credentials
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError

    tw, hecha = await _sandbox(negocio, twilio)
    gate(False)
    r, sid = await _post(app_client, hecha, tw, "inbound")
    assert r.status_code == 200 and "<Response/>" in r.text
    assert await _encolados(sid) == 0
    assert await _estado(negocio, hecha.connection_id) == ("pending", None)

    proveedor, creds = await _en(
        negocio, resolve_credentials, connection_id=hecha.connection_id, expected_tenant_id=negocio
    )
    assert (proveedor, creds) == ("twilio", {})
    with pytest.raises(AltaError) as exc:
        await _en(negocio, operacion.enviar_prueba, negocio, hecha.connection_id, "+573001234567")
    assert exc.value.http_status == 404
    # La salud no le habla a Twilio y la conexión se sigue pudiendo leer.
    llamadas = len(tw.enviados)
    leida = await _en(negocio, operacion.revisar, negocio, hecha.connection_id)
    assert leida is not None and leida.modo == "sandbox" and len(tw.enviados) == llamadas


async def test_salud_solo_mira_la_cuenta_y_la_confirmacion(
    negocio, app_client, gate, twilio
) -> None:
    from core.gateway.twilio import operacion

    tw, hecha = await _sandbox(negocio, twilio)
    cid = hecha.connection_id
    assert (await _en(negocio, operacion.revisar, negocio, cid)).status == "pending"
    r, _ = await _post(app_client, hecha, tw, "inbound")
    assert r.status_code == 200
    assert (await _en(negocio, operacion.revisar, negocio, cid)).status == "connected"
    tw.status = "suspended"
    assert (await _en(negocio, operacion.revisar, negocio, cid)).status == "revoked"
    assert tw.senders == [], "la salud del sandbox nunca llama a la Senders API"


async def test_reregistrar_es_409_con_las_urls_y_desconectar_no_restaura(
    negocio, app_client, gate, twilio
) -> None:
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError

    tw, hecha = await _sandbox(negocio, twilio)
    with pytest.raises(AltaError) as exc:
        await _en(negocio, operacion.reregistrar_webhooks, negocio, hecha.connection_id)
    assert (exc.value.http_status, exc.value.code) == (409, "sandbox_webhook_manual")
    assert exc.value.details == {
        "webhook_inbound_url": hecha.webhook_inbound_url,
        "webhook_status_url": hecha.webhook_status_url,
    }

    h = await api_headers(app_client, negocio)
    r = await app_client.post(
        f"/api/v1/admin/connections/twilio/{hecha.connection_id}/webhooks", headers=h
    )
    assert (
        r.status_code == 409
        and r.json()["error"]["details"]["webhook_status_url"] == hecha.webhook_status_url
    )

    from _api_helpers import admin_password

    r = await app_client.post(
        f"/api/v1/admin/connections/twilio/{hecha.connection_id}/disconnect",
        headers=h,
        json={"password": admin_password(), "restore_webhooks": True},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"webhooks_restaurados": False, "requiere_limpiar_consola": True}
    assert tw.senders == []
    assert (
        await _sql(
            negocio,
            "select 1 from twilio_connections where channel_connection_id = :c and deleted_at is null",
            c=hecha.connection_id,
        )
        == []
    )
    # El webhook de una conexión desconectada ya no rutea.
    r, _ = await _post(app_client, hecha, tw, "inbound")
    assert r.status_code == 404


async def test_prueba_a_un_participante_no_unido_es_422(negocio, gate, twilio) -> None:
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError

    tw, hecha = await _sandbox(negocio, twilio)
    tw.error_envio = 63015
    with pytest.raises(AltaError) as exc:
        await _en(negocio, operacion.enviar_prueba, negocio, hecha.connection_id, "+573001234567")
    assert (exc.value.http_status, exc.value.code) == (422, "participante_no_unido")
    assert exc.value.details == {"join": "join cloud-forgotten", "sandbox_e164": SANDBOX}
    assert "join cloud-forgotten" in exc.value.message


async def test_prueba_sale_del_numero_del_sandbox(negocio, gate, twilio) -> None:
    from core.gateway.twilio import operacion

    tw, hecha = await _sandbox(negocio, twilio)
    await _en(negocio, operacion.enviar_prueba, negocio, hecha.connection_id, "+573001234567")
    [enviado] = tw.enviados
    assert enviado["From"] == f"whatsapp:{SANDBOX}" and enviado["To"] == "whatsapp:+573001234567"


async def test_rotar_revalida_la_cuenta_y_no_cambia_de_cuenta(negocio, gate, twilio) -> None:
    from core.gateway.twilio import operacion
    from core.gateway.twilio.alta import AltaError

    tw, hecha = await _sandbox(negocio, twilio)
    with pytest.raises(AltaError) as exc:
        await _en(
            negocio,
            operacion.rotar_credenciales,
            negocio,
            hecha.connection_id,
            account_sid=_ac(),
            auth_token="nuevo",
        )
    assert exc.value.code == "sandbox_otra_cuenta"
    rotada = await _en(
        negocio,
        operacion.rotar_credenciales,
        negocio,
        hecha.connection_id,
        account_sid=tw.ac,
        auth_token="nuevo",
    )
    assert rotada.connection_id == hecha.connection_id
    assert rotada.webhook_inbound_url == hecha.webhook_inbound_url
    assert rotada.sandbox_join_code == "cloud-forgotten"
    assert tw.senders == []


async def test_plantillas_propias_no_aplican_al_sandbox(negocio, gate, twilio) -> None:
    from core.gateway.twilio import plantillas
    from core.gateway.twilio.alta import AltaError

    _, hecha = await _sandbox(negocio, twilio)
    cid = hecha.connection_id
    llamadas = [
        lambda: plantillas.crear(
            negocio,
            cid,
            nombre="saludo",
            idioma="es",
            categoria="UTILITY",
            cuerpo="Hola {{1}}",
            ejemplos={"1": "Ana"},
        ),
        lambda: plantillas.enviar_a_aprobacion(negocio, cid, uuid.uuid4()),
        lambda: plantillas.borrar(negocio, cid, uuid.uuid4()),
    ]
    for llamada in llamadas:
        with pytest.raises(AltaError) as exc:
            await _en(negocio, llamada)
        assert (exc.value.http_status, exc.value.code) == (409, "no_aplica_sandbox")

"""HU-CONN-20 (adenda del ADR-0186) · alta del Sandbox de Twilio contra Postgres real, con Twilio simulado.

- Gate de ambiente: con `twilio_sandbox_permitido` apagado, el servicio y las rutas responden 404 (igual que sin la
  bandera del negocio). `GET /capabilities` dice si el ambiente lo permite.
- El alta valida solo la cuenta (`GET /Accounts/{sid}`, nunca la Senders API), guarda `sandbox:<AccountSid>` y el
  número compartido como `From`, y queda `pending` con `requiere_accion_manual`. Repetirla (también en paralelo)
  devuelve la misma conexión y el mismo `public_id`.
- La misma cuenta en otro negocio: 409 `cuenta_en_otro_negocio`. RLS: el otro negocio no la ve.
- Permisos: propietario o master (un colaborador con escritura no conecta), cuerpo cerrado (campo extra 422).

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
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

SANDBOX = "+14155238886"
TOKEN = "token-secreto"
BASE = "/api/v1/admin/connections/twilio"


def _cuenta() -> str:
    return "AC" + uuid.uuid4().hex


class CuentaFalsa:
    """Una cuenta de Twilio: responde `GET /Accounts/{sid}` y cuenta cualquier otra llamada (no debe haber)."""

    def __init__(self, account_sid: str, *, status: str = "active") -> None:
        self.account_sid = account_sid
        self.status = status
        self.otras: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/Accounts/{self.account_sid}.json"):
            return httpx.Response(
                200, json={"status": self.status, "friendly_name": "Cuenta", "type": "Trial"}
            )
        self.otras.append(request.url.path)
        return httpx.Response(404, json={"code": 20404})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from _habilidades import onboard_tenant

    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo in ("sba", "sbb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(
            plan="empresarial",  # todos los canales y cupo de conexiones (HU-BILL-19)
            name=f"Sandbox {slug}", slug=slug, packs=[], sembrar_ejemplos=False
        )
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        async with tenant_scope(tid):
            await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


@pytest.fixture
def gate(monkeypatch):
    """Enciende o apaga `TWILIO_SANDBOX_PERMITIDO` para la prueba."""
    from core.config import get_settings

    def fijar(valor: bool) -> None:
        monkeypatch.setattr(get_settings(), "twilio_sandbox_permitido", valor)

    fijar(True)
    return fijar


async def _bandera(tid: uuid.UUID, activa: bool) -> None:
    from core.gateway.twilio import bandera
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await bandera.fijar(tid, "whatsapp_twilio", activa, actor="test")


async def _sandbox(tid: uuid.UUID, cuenta: CuentaFalsa, **extra: Any):
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await alta.conectar_sandbox(
            tid,
            account_sid=cuenta.account_sid,
            auth_token=TOKEN,
            transport=cuenta.transport,
            **extra,
        )


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if resultado.returns_rows else []


def _falsear_cliente(monkeypatch, cuenta: CuentaFalsa) -> None:
    from agentcore_channel_twilio.client import TwilioClient

    from core.gateway.twilio import alta

    monkeypatch.setattr(
        alta,
        "_cliente",
        lambda sid, token, transport: TwilioClient(sid, token, transport=cuenta.transport),
    )


async def test_con_el_gate_apagado_el_servicio_responde_404(negocios, gate) -> None:
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    gate(False)
    cuenta = CuentaFalsa(_cuenta())
    with pytest.raises(AltaError) as exc:
        await _sandbox(tid, cuenta)
    assert exc.value.http_status == 404
    assert cuenta.otras == []
    assert (
        await _sql(
            tid,
            "select 1 from channel_connections where external_id = :e",
            e=f"sandbox:{cuenta.account_sid}",
        )
        == []
    )


async def test_alta_guarda_pending_y_es_idempotente(negocios, gate) -> None:
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    cuenta = CuentaFalsa(_cuenta())
    primera = await _sandbox(tid, cuenta, join_code="Cloud-Forgotten", label="Pruebas")

    assert primera.created is True
    assert (primera.modo, primera.status, primera.sender_e164) == ("sandbox", "pending", SANDBOX)
    assert primera.sender_sid == "" and primera.sandbox_join_code == "cloud-forgotten"
    assert primera.requiere_accion_manual is True and primera.webhook_confirmado_at is None
    assert "/webhooks/twilio/" in primera.webhook_inbound_url
    assert cuenta.otras == [], "el alta del sandbox no llama a la Senders API"

    [(external_id, cifradas, label)] = await _sql(
        tid,
        "select external_id, credentials_encrypted, label from channel_connections where id = :c",
        c=primera.connection_id,
    )
    assert external_id == f"sandbox:{cuenta.account_sid}" and label == "Pruebas"
    assert TOKEN not in (cifradas or "")

    # Repetir sin código conserva el anterior; con código lo cambia. Mismo `public_id`.
    segunda = await _sandbox(tid, cuenta)
    assert segunda.created is False and segunda.connection_id == primera.connection_id
    assert segunda.webhook_inbound_url == primera.webhook_inbound_url
    assert segunda.sandbox_join_code == "cloud-forgotten"
    tercera = await _sandbox(tid, cuenta, join_code="otra-cosa")
    assert tercera.sandbox_join_code == "otra-cosa"
    assert await _sql(
        tid,
        "select count(*) from twilio_connections where channel_connection_id = :c",
        c=primera.connection_id,
    ) == [(1,)]

    # El envío sale del número compartido.
    async with tenant_scope(tid):
        proveedor, creds = await resolve_credentials(
            connection_id=primera.connection_id, expected_tenant_id=tid
        )
    assert proveedor == "twilio" and creds["from"] == f"whatsapp:{SANDBOX}"


async def test_altas_en_paralelo_dejan_una_sola_conexion(negocios, gate) -> None:
    tid, _ = negocios
    cuenta = CuentaFalsa(_cuenta())
    resultados = await asyncio.gather(*(_sandbox(tid, cuenta) for _ in range(4)))
    assert len({r.connection_id for r in resultados}) == 1
    assert sum(r.created for r in resultados) == 1
    assert await _sql(
        tid,
        "select count(*) from twilio_connections where account_sid = :a and deleted_at is null",
        a=cuenta.account_sid,
    ) == [(1,)]


async def test_la_misma_cuenta_en_otro_negocio_es_409_y_rls(negocios, gate) -> None:
    from core.gateway.twilio.alta import AltaError

    tid_a, tid_b = negocios
    cuenta = CuentaFalsa(_cuenta())
    hecha = await _sandbox(tid_a, cuenta)
    with pytest.raises(AltaError) as exc:
        await _sandbox(tid_b, cuenta)
    assert (exc.value.http_status, exc.value.code) == (409, "cuenta_en_otro_negocio")
    assert (
        await _sql(
            tid_b,
            "select 1 from twilio_connections where channel_connection_id = :c",
            c=hecha.connection_id,
        )
        == []
    )


async def test_cuenta_suspendida_codigo_invalido_y_fuera_del_scope(negocios, gate) -> None:
    from core.gateway.twilio import alta
    from core.gateway.twilio.alta import AltaError
    from core.shared.tenant import tenant_scope

    tid_a, tid_b = negocios
    with pytest.raises(AltaError) as exc:
        await _sandbox(tid_a, CuentaFalsa(_cuenta(), status="suspended"))
    assert exc.value.code == "twilio_account_suspended"
    with pytest.raises(AltaError) as exc:
        await _sandbox(tid_a, CuentaFalsa(_cuenta()), join_code="join cloud")
    assert exc.value.code == "join_code_invalido"

    cuenta = CuentaFalsa(_cuenta())
    async with tenant_scope(tid_b):
        with pytest.raises(PermissionError):
            await alta.conectar_sandbox(
                tid_a, account_sid=cuenta.account_sid, auth_token=TOKEN, transport=cuenta.transport
            )


async def test_api_capacidades_alta_y_codigo(negocios, app_client, gate, monkeypatch) -> None:
    tid, _ = negocios
    cuenta = CuentaFalsa(_cuenta())
    _falsear_cliente(monkeypatch, cuenta)
    h = await api_headers(app_client, tid)
    cuerpo = {
        "account_sid": cuenta.account_sid,
        "auth_token": TOKEN,
        "join_code": "cloud-forgotten",
    }

    # Sin la bandera del negocio: 404 en todo.
    await _bandera(tid, False)
    assert (await app_client.get(f"{BASE}/capabilities", headers=h)).status_code == 404
    assert (await app_client.post(f"{BASE}/sandbox", headers=h, json=cuerpo)).status_code == 404

    await _bandera(tid, True)
    try:
        gate(False)
        r = await app_client.get(f"{BASE}/capabilities", headers=h)
        assert r.status_code == 200 and r.json() == {
            "sandbox_disponible": False,
            "sandbox_e164": "",
        }
        r = await app_client.post(f"{BASE}/sandbox", headers=h, json=cuerpo)
        assert r.status_code == 404, r.text

        gate(True)
        r = await app_client.get(f"{BASE}/capabilities", headers=h)
        assert r.json() == {"sandbox_disponible": True, "sandbox_e164": SANDBOX}

        r = await app_client.post(
            f"{BASE}/sandbox", headers=h, json={**cuerpo, "sender_sid": "XE1"}
        )
        assert r.status_code == 422, "cuerpo cerrado"
        r = await app_client.post(
            f"{BASE}/sandbox", headers=h, json={**cuerpo, "join_code": "Join X"}
        )
        assert r.status_code == 422

        r = await app_client.post(f"{BASE}/sandbox", headers=h, json=cuerpo)
        assert r.status_code == 201, r.text
        creada = r.json()
        assert creada["modo"] == "sandbox" and creada["status"] == "pending"
        assert creada["requiere_accion_manual"] is True and creada["sender_sid"] == ""
        assert creada["sandbox_join_code"] == "cloud-forgotten" and TOKEN not in r.text
        r = await app_client.post(f"{BASE}/sandbox", headers=h, json=cuerpo)
        assert r.status_code == 200 and r.json()["connection_id"] == creada["connection_id"]

        r = await app_client.get(f"{BASE}/{creada['connection_id']}", headers=h)
        assert r.status_code == 200 and r.json()["modo"] == "sandbox"

        r = await app_client.patch(
            f"{BASE}/{creada['connection_id']}/sandbox",
            headers=h,
            json={"join_code": "nuevo-codigo"},
        )
        assert r.status_code == 200 and r.json()["sandbox_join_code"] == "nuevo-codigo", r.text
        r = await app_client.patch(
            f"{BASE}/{uuid.uuid4()}/sandbox", headers=h, json={"join_code": "x"}
        )
        assert r.status_code == 404

        gate(False)
        r = await app_client.patch(
            f"{BASE}/{creada['connection_id']}/sandbox", headers=h, json={"join_code": "otro"}
        )
        assert r.status_code == 404
        # Con el gate apagado, la conexión se sigue pudiendo leer.
        r = await app_client.get(f"{BASE}/{creada['connection_id']}", headers=h)
        assert r.status_code == 200
    finally:
        await _bandera(tid, False)


async def test_api_colaborador_no_conecta_el_sandbox(
    negocios, app_client, gate, monkeypatch
) -> None:
    from test_opciones_api import _headers

    tid, _ = negocios
    cuenta = CuentaFalsa(_cuenta())
    _falsear_cliente(monkeypatch, cuenta)
    await _bandera(tid, True)
    try:
        h = await _headers(app_client, tid, rol="colaborador", secciones={"connections": "write"})
        r = await app_client.post(
            f"{BASE}/sandbox",
            headers=h,
            json={"account_sid": cuenta.account_sid, "auth_token": TOKEN},
        )
        assert r.status_code == 403, r.text
        assert (
            await _sql(
                tid,
                "select 1 from channel_connections where external_id = :e",
                e=f"sandbox:{cuenta.account_sid}",
            )
            == []
        )
    finally:
        await _bandera(tid, False)


async def test_patch_sobre_un_sender_es_409(negocios, gate) -> None:
    from test_twilio_alta_db import XE, TwilioFalso

    from core.gateway.twilio import alta
    from core.gateway.twilio.alta import AltaError
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    falso = TwilioFalso(f"+1555{uuid.uuid4().int % 10_000_000:07d}", account_sid=_cuenta())
    async with tenant_scope(tid):
        sender = await alta.conectar(
            tid,
            account_sid=falso.account_sid,
            auth_token=TOKEN,
            sender_sid=XE,
            transport=falso.transport,
        )
        assert sender.modo == "sender" and sender.requiere_accion_manual is False
        with pytest.raises(AltaError) as exc:
            await alta.fijar_join_code(tid, sender.connection_id, "algo")
    assert (exc.value.http_status, exc.value.code) == (409, "no_es_sandbox")


async def test_patch_con_el_id_de_otro_negocio_es_404(negocios, app_client, gate) -> None:
    tid_a, tid_b = negocios
    de_b = await _sandbox(tid_b, CuentaFalsa(_cuenta()), join_code="codigo-de-b")
    await _bandera(tid_a, True)
    try:
        h = await api_headers(app_client, tid_a)
        r = await app_client.patch(
            f"{BASE}/{de_b.connection_id}/sandbox", headers=h, json={"join_code": "robado"}
        )
        assert r.status_code == 404, r.text
    finally:
        await _bandera(tid_a, False)
    assert await _sql(
        tid_b,
        "select sandbox_join_code from twilio_connections where channel_connection_id = :c",
        c=de_b.connection_id,
    ) == [("codigo-de-b",)]


async def test_repetir_el_alta_no_reactiva_una_conexion_pausada(negocios, gate) -> None:
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    cuenta = CuentaFalsa(_cuenta())
    hecha = await _sandbox(tid, cuenta)
    await _sql(
        tid, "update channel_connections set active = false where id = :c", c=hecha.connection_id
    )
    with pytest.raises(AltaError) as exc:
        await _sandbox(tid, cuenta)
    assert (exc.value.http_status, exc.value.code) == (409, "conexion_pausada")
    assert await _sql(
        tid, "select active from channel_connections where id = :c", c=hecha.connection_id
    ) == [(False,)]

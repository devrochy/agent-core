"""ADR-0186 (PR-7b) · envío de una plantilla a un segmento por una conexión de Twilio, contra Postgres real.

- La conexión de Twilio aparece con sus plantillas de marketing aprobadas y de texto, y no con las demás.
- Vista previa, confirmación y lotes como en Meta; cada mensaje lleva el `ContentSid` y sale por Twilio.
- Reprocesar un lote no duplica; una plantilla que deja de estar aprobada o un cliente que escribió por otra conexión
  se omiten.
- El 21610 de Twilio da de baja al cliente una sola vez, no escala y no se deshace desde la ficha.
- Otra conexión o negocio: 404; conexión pausada: 409.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/leads"
ACC = f"{URL}/acciones/plantilla"
XE = "XE" + "d" * 32
VARIABLES = {
    "1": {"fuente": "cliente.nombre", "valor": "amigo"},
    "2": {"fuente": "fijo", "valor": "2x1 en cortes"},
}


@pytest.fixture(autouse=True)
def _sin_guarda_de_test(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.admin import envios_segmento

    monkeypatch.setattr(envios_segmento, "lista_permitida", lambda: None)


@pytest.fixture
def encolados(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    cola: list[str] = []
    monkeypatch.setattr(
        "core.tasks.outbound_send.outbound_send_task.delay", lambda mid, tid: cola.append(str(mid))
    )
    return cola


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


def _plantilla(tid: uuid.UUID, conexion: uuid.UUID, nombre: str, **extra: Any) -> Any:
    from core.db.models import TwilioContentTemplate

    cuerpo = "Hola {{1}}, esta semana {{2}}."
    datos: dict[str, Any] = {
        "tenant_id": tid,
        "channel_connection_id": conexion,
        "content_sid": "HX" + uuid.uuid4().hex,
        "friendly_name": nombre,
        "whatsapp_name": nombre,
        "language": "es",
        "category": "MARKETING",
        "body": cuerpo,
        "types": {"twilio/text": {"body": cuerpo}},
        "approval_status": "approved",
    }
    return TwilioContentTemplate(**{**datos, **extra})


async def _negocio() -> dict[str, Any]:
    """Negocio con una conexión de Twilio, plantillas de cada clase y dos clientes con consentimiento (ana, eva)."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import Conversation, Customer, CustomerConsent
    from core.db.session import get_session
    from core.gateway.twilio import alta, bandera
    from core.shared.tenant import tenant_scope

    slug = uniq("twseg")
    tid, _ = await onboard_tenant(
        name=f"Segmentos {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    ac = "AC" + uuid.uuid4().hex
    e164 = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    ids: dict[str, Any] = {"tid": tid, "ac": ac}
    async with tenant_scope(tid):
        await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
        hecha = await alta.conectar(tid, account_sid=ac, auth_token="tok", sender_sid=XE, transport=_alta(ac, e164))
        ids["conexion"] = hecha.connection_id
        async with get_session() as s:
            promo = _plantilla(tid, hecha.connection_id, "promo_semana")
            s.add_all(
                [
                    promo,
                    _plantilla(tid, hecha.connection_id, "aviso_pedido", category="UTILITY"),
                    _plantilla(tid, hecha.connection_id, "promo_pendiente", approval_status="pending"),
                    _plantilla(
                        tid,
                        hecha.connection_id,
                        "promo_botones",
                        types={"twilio/quick-reply": {"body": "Hola {{1}}", "actions": []}},
                    ),
                ]
            )
            await s.flush()
            ids.update(promo=promo.id, sid=promo.content_sid)
            for nombre in ("ana", "eva"):
                c = Customer(
                    tenant_id=tid,
                    channel="whatsapp",
                    external_id=f"whatsapp:+57300{uuid.uuid4().int % 10_000_000:07d}",
                    name=nombre.title(),
                )
                s.add(c)
                await s.flush()
                conv = Conversation(tenant_id=tid, customer_id=c.id, channel="whatsapp", connection_id=hecha.connection_id)
                s.add(conv)
                await s.flush()
                s.add(CustomerConsent(tenant_id=tid, customer_id=c.id, estado="otorgado", origen="ficha"))
                await s.flush()
                ids[nombre], ids[f"conv_{nombre}"] = c.id, conv.id
    return ids


def _cuerpo(n: dict[str, Any], template_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "connection_id": str(n["conexion"]),
        "template_id": str(template_id or n["promo"]),
        "variables": VARIABLES,
    }


async def _lotes(tid: uuid.UUID, send_id: str) -> int:
    from sqlalchemy import select

    from core.admin.envios_segmento import procesar_lote
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    entregados = 0
    async with tenant_scope(tid):
        while True:
            async with get_session() as session:
                evento = (
                    await session.execute(
                        select(DomainEvent).where(
                            DomainEvent.tenant_id == tid,
                            DomainEvent.status == "pendiente",
                            DomainEvent.dedupe_key.like(f"segmento:{send_id}:lote:%"),
                        )
                    )
                ).scalars().first()
                if evento is None:
                    return entregados
                await procesar_lote(**{k: v for k, v in evento.payload.items() if k in ("send_id", "lote")})
                evento.status = "entregado"
                entregados += 1


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            r = await session.execute(text(sql), params)
            return list(r.all()) if r.returns_rows else []


async def _confirmado(app_client, n: dict[str, Any], h: dict[str, str]) -> str:
    envio = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(n), headers=h)).json()["envio_id"]
    r = await app_client.post(f"{ACC}/{envio}/confirmar", headers={**h, "Idempotency-Key": uniq("k")})
    assert r.status_code == 202, r.text
    return envio


async def test_lista_solo_las_de_marketing_aprobadas_y_de_texto(app_client) -> None:
    n = await _negocio()
    h = await api_headers(app_client, n["tid"])
    r = await app_client.get(f"{ACC}/conexiones", headers=h)
    assert r.status_code == 200, r.text
    [conexion] = r.json()
    assert conexion["id"] == str(n["conexion"]) and conexion["motivo_pausa"] is None
    [plantilla] = conexion["plantillas"]
    assert plantilla["id"] == str(n["promo"]) and plantilla["name"] == "promo_semana"
    assert plantilla["variables"] == ["1", "2"] and plantilla["body"] == "Hola {{1}}, esta semana {{2}}."


async def test_envio_completo_por_twilio(app_client, encolados, monkeypatch) -> None:
    from agentcore_channel_twilio.adapters import TwilioOutbound

    from core.tasks import outbound_send

    n = await _negocio()
    h = await api_headers(app_client, n["tid"])
    r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(n), headers=h)
    assert r.status_code == 200, r.text
    vista = r.json()
    assert vista["elegibles"] == 2 and vista["bloqueo"] is None
    assert vista["texto"] == "Hola amigo, esta semana 2x1 en cortes."

    r = await app_client.post(
        f"{ACC}/{vista['envio_id']}/confirmar", headers={**h, "Idempotency-Key": uniq("k")}
    )
    assert r.status_code == 202, r.text
    assert await _lotes(n["tid"], vista["envio_id"]) == 1
    assert len(encolados) == 2

    filas = await _sql(
        n["tid"],
        "select template_name, template_components, content, client_message_id, id from messages "
        "where conversation_id = :c and direction = 'outbound'",
        c=n["conv_ana"],
    )
    [(nombre, comps, contenido, cmid, message_id)] = filas
    assert nombre == "promo_semana" and cmid == f"segmento:{vista['envio_id']}:{n['ana']}"
    assert comps == [
        {
            "type": "twilio_content",
            "content_sid": n["sid"],
            "variables": {"1": "Ana", "2": "2x1 en cortes"},
        }
    ]
    assert contenido == "Hola Ana, esta semana 2x1 en cortes."

    # Y sale de verdad por Twilio, con su ContentSid.
    enviados: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        enviados.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(201, json={"sid": "SM" + uuid.uuid4().hex})

    adapter = TwilioOutbound(httpx.MockTransport(handler))
    monkeypatch.setattr(outbound_send, "outbound_adapter_for", lambda provider, channel=None: adapter)
    assert await outbound_send._send_one(message_id, n["tid"]) is None
    [form] = enviados
    assert form["ContentSid"] == n["sid"] and json.loads(form["ContentVariables"])["1"] == "Ana"

    # Reprocesar un lote ya entregado no duplica.
    from core.admin.envios_segmento import procesar_lote
    from core.shared.tenant import tenant_scope

    async with tenant_scope(n["tid"]):
        await procesar_lote(send_id=vista["envio_id"], lote=1)
    assert len(encolados) == 2
    detalle = (await app_client.get(f"{ACC}/{vista['envio_id']}", headers=h)).json()
    assert detalle["estado"] == "terminado" and detalle["plantilla"] == "promo_semana"


async def test_plantilla_que_deja_de_estar_aprobada_se_omite(app_client, encolados) -> None:
    n = await _negocio()
    h = await api_headers(app_client, n["tid"])
    envio = await _confirmado(app_client, n, h)
    await _sql(
        n["tid"], "update twilio_content_templates set approval_status = 'paused' where id = :i", i=n["promo"]
    )
    assert await _lotes(n["tid"], envio) == 1
    assert encolados == []
    motivos = await _sql(
        n["tid"], "select distinct motivo from segment_send_recipients where send_id = :s", s=envio
    )
    assert motivos == [("plantilla_no_aprobada",)]


async def test_cliente_que_escribio_por_otra_conexion_se_omite(app_client, encolados) -> None:
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    h = await api_headers(app_client, n["tid"])
    envio = await _confirmado(app_client, n, h)
    ac = "AC" + uuid.uuid4().hex
    e164 = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    async with tenant_scope(n["tid"]):
        otra = await alta.conectar(n["tid"], account_sid=ac, auth_token="tok", sender_sid=XE, transport=_alta(ac, e164))
    await _sql(n["tid"], "update conversations set connection_id = :o where id = :c", o=otra.connection_id, c=n["conv_eva"])

    assert await _lotes(n["tid"], envio) == 1
    assert len(encolados) == 1  # solo ana
    motivos = await _sql(
        n["tid"],
        "select motivo from segment_send_recipients where send_id = :s and customer_id = :c",
        s=envio,
        c=n["eva"],
    )
    assert motivos == [("otra_conexion",)]


async def test_plantillas_ajenas_y_conexion_pausada(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    h = await api_headers(app_client, a["tid"])
    # La plantilla de utilidad, la pendiente o la de otro negocio no sirven.
    [(utilidad,)] = await _sql(
        a["tid"], "select id from twilio_content_templates where friendly_name = 'aviso_pedido' and tenant_id = :t", t=a["tid"]
    )
    for plantilla in (utilidad, b["promo"]):
        r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(a, plantilla), headers=h)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "template_not_found"), r.text
    # Tampoco la de otra conexión de Twilio del mismo negocio.
    from core.db.session import get_session
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    ac = "AC" + uuid.uuid4().hex
    e164 = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    async with tenant_scope(a["tid"]):
        otra = await alta.conectar(a["tid"], account_sid=ac, auth_token="tok", sender_sid=XE, transport=_alta(ac, e164))
        async with get_session() as s:
            ajena = _plantilla(a["tid"], otra.connection_id, "promo_de_la_otra")
            s.add(ajena)
            await s.flush()
            ajena_id = ajena.id
    r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(a, ajena_id), headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "template_not_found"), r.text
    # La conexión de otro negocio no existe para este.
    r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(b), headers=h)
    assert r.status_code == 404, r.text

    await _sql(a["tid"], "update channel_connections set active = false where id = :c", c=a["conexion"])
    r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(a), headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "connection_paused"), r.text
    conexion = next(
        c for c in (await app_client.get(f"{ACC}/conexiones", headers=h)).json() if c["id"] == str(a["conexion"])
    )
    assert conexion["motivo_pausa"]


async def test_21610_da_de_baja_una_vez_y_no_escala(app_client, monkeypatch) -> None:
    from core.admin.envios_segmento import client_message_id
    from core.db.models import Message
    from core.db.session import get_session
    from core.gateway.channels.base import ProviderError
    from core.orchestrator import escalation
    from core.shared.tenant import tenant_scope
    from core.tasks import outbound_send

    escaladas: list[uuid.UUID] = []

    async def escalar(conversation_id, reason, summary=""):
        escaladas.append(conversation_id)

    monkeypatch.setattr(escalation, "escalate", escalar)
    n = await _negocio()
    h = await api_headers(app_client, n["tid"])
    mensajes = []
    async with tenant_scope(n["tid"]):
        async with get_session() as s:
            for i in range(2):
                m = Message(
                    tenant_id=n["tid"], conversation_id=n["conv_ana"], direction="outbound", sender_type="human",
                    content="promo", status="queued", template_name="promo_semana",
                    client_message_id=client_message_id(uuid.uuid4(), n["ana"]) + f":{i}",
                )
                s.add(m)
                await s.flush()
                mensajes.append(m.id)
    error = ProviderError("El cliente se dio de baja", provider="twilio", code=21610, category="OPT_OUT")
    for mid in mensajes:
        await outbound_send._mark_failed(mid, n["tid"], error, dlq=False)

    assert escaladas == []
    bajas = await _sql(
        n["tid"],
        "select estado, origen from customer_consents where customer_id = :c and origen = 'twilio_21610'",
        c=n["ana"],
    )
    assert bajas == [("revocado", "twilio_21610")]

    # Queda fuera de la próxima vista previa, y nadie la reactiva desde la ficha.
    vista = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(n), headers=h)).json()
    assert {e["motivo"]: e["total"] for e in vista["excluidos"]} == {"baja": 1}
    historial = (await app_client.get(f"{URL}/{n['ana']}/consentimiento", headers=h)).json()["historial"]
    assert next(f for f in historial if f["origen"] == "twilio_21610")["quien"] == "el cliente"
    r = await app_client.post(
        f"{URL}/{n['ana']}/consentimiento",
        headers=h,
        json={"estado": "otorgado", "como": "por_telefono", "evidencia": "dijo que sí"},
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "baja_del_cliente"), r.text

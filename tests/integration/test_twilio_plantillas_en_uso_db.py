"""ADR-0186 (PR-7a) · plantillas de Twilio en el chat y en los avisos por evento, contra Postgres real.

- Selector del chat: lista las aprobadas de la conexión de Twilio de la conversación y encola la elegida con su
  `ContentSid` y variables; faltan variables → 400; un id de otra conexión → 404.
- Mapa de avisos: acepta el nombre de una plantilla de Twilio y valida sus variables.
- Aviso por evento con la ventana cerrada: sale la plantilla de Twilio con los valores del servidor.
- Aislamiento: otro negocio no ve la conversación ni sus plantillas.

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

XE = "XE" + "b" * 32
EVENTO = "appointment.reminder"


def _twilio(ac: str, e164: str) -> httpx.MockTransport:
    estado: dict[str, Any] = {"webhook": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/Accounts/{ac}.json"):
            return httpx.Response(
                200, json={"status": "active", "friendly_name": "C", "type": "Full"}
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


async def _negocio_con_twilio(prefijo: str) -> dict[str, Any]:
    from core.alta_negocio import onboard_tenant
    from core.db.models import Conversation, Customer, TwilioContentTemplate
    from core.db.session import get_session
    from core.gateway.twilio import alta, bandera
    from core.shared.tenant import tenant_scope

    slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"Twilio {slug}", slug=slug, packs=[], sembrar_ejemplos=False
    )
    ac = "AC" + uuid.uuid4().hex
    e164 = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    async with tenant_scope(tid):
        await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
        hecha = await alta.conectar(
            tid, account_sid=ac, auth_token="tok", sender_sid=XE, transport=_twilio(ac, e164)
        )
        async with get_session() as s:
            plantilla = TwilioContentTemplate(
                tenant_id=tid,
                channel_connection_id=hecha.connection_id,
                content_sid="HX" + uuid.uuid4().hex,
                friendly_name="recordatorio_cita",
                whatsapp_name="recordatorio_cita",
                language="es",
                category="UTILITY",
                body="Hola {{1}}, tu cita es el {{2}}",
                types={"twilio/text": {"body": "Hola {{1}}, tu cita es el {{2}}"}},
                variables={"1": "Ana", "2": "lunes"},
                approval_status="approved",
            )
            s.add(plantilla)
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id="whatsapp:+573001234567", name="Ana"
            )
            s.add(cliente)
            await s.flush()
            conv = Conversation(
                tenant_id=tid,
                customer_id=cliente.id,
                channel="whatsapp",
                connection_id=hecha.connection_id,
            )
            s.add(conv)
            await s.flush()
            ids = {"plantilla": plantilla.id, "conversacion": conv.id, "sid": plantilla.content_sid}
    return {"tid": tid, "slug": slug, "connection_id": hecha.connection_id, **ids}


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[dict[str, Any], dict[str, Any]]]:
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    a = await _negocio_con_twilio("tua")
    b = await _negocio_con_twilio("tub")
    yield a, b
    for n in (a, b):
        async with tenant_scope(n["tid"]):
            await deactivate_tenant(str(n["tid"]))
        await purge_tenant(str(n["tid"]), f"ELIMINAR {n['slug']}")


@pytest.fixture
def encolados(monkeypatch) -> list[str]:
    cola: list[str] = []
    monkeypatch.setattr(
        "core.tasks.outbound_send.outbound_send_task.delay", lambda mid, tid: cola.append(str(mid))
    )
    return cola


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            r = await session.execute(text(sql), params)
            return list(r.all()) if r.returns_rows else []


async def test_selector_del_chat(negocios, app_client, encolados) -> None:
    a, b = negocios
    h = await api_headers(app_client, a["tid"])
    base = f"/api/v1/admin/conversations/{a['conversacion']}"
    r = await app_client.get(f"{base}/templates", headers=h)
    assert r.status_code == 200, r.text
    assert [(t["id"], t["name"], t["variables"]) for t in r.json()] == [
        (str(a["plantilla"]), "recordatorio_cita", ["1", "2"])
    ]

    r = await app_client.post(
        f"{base}/template-messages",
        headers=h,
        json={"template_id": str(a["plantilla"]), "variables": {"1": "Ana"}},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "template_variables"

    r = await app_client.post(
        f"{base}/template-messages",
        headers=h,
        json={"template_id": str(a["plantilla"]), "variables": {"1": "Ana", "2": "lunes"}},
    )
    assert r.status_code == 204, r.text
    [(nombre, comps, contenido, sender)] = await _sql(
        a["tid"],
        "select template_name, template_components, content, sender_type from messages where conversation_id = :c "
        "and template_name is not null order by created_at desc limit 1",
        c=a["conversacion"],
    )
    assert nombre == "recordatorio_cita" and sender == "human"
    assert comps == [
        {"type": "twilio_content", "content_sid": a["sid"], "variables": {"1": "Ana", "2": "lunes"}}
    ]
    assert contenido == "Hola Ana, tu cita es el lunes"
    assert len(encolados) == 1

    # Una plantilla de otra conexión (de B) no sirve aquí.
    r = await app_client.post(
        f"{base}/template-messages",
        headers=h,
        json={"template_id": str(b["plantilla"]), "variables": {"1": "Ana", "2": "lunes"}},
    )
    assert r.status_code == 404


async def test_otro_negocio_no_ve_la_conversacion(negocios, app_client) -> None:
    a, b = negocios
    h_b = await api_headers(app_client, b["tid"])
    r = await app_client.get(
        f"/api/v1/admin/conversations/{a['conversacion']}/templates", headers=h_b
    )
    assert r.status_code == 404


async def test_mapa_y_aviso_por_evento_con_ventana_cerrada(negocios, app_client, encolados) -> None:
    from core.gateway.event_notices import send_event_notice
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    h = await api_headers(app_client, a["tid"])
    url = "/api/v1/admin/settings/message-templates"
    malo = {"template": "recordatorio_cita", "language": "es", "variables": ["cliente.nombre"]}
    r = await app_client.put(url, headers=h, json={"mapping": {EVENTO: malo}})
    assert r.status_code == 400, r.text  # la plantilla tiene 2 variables
    bueno = {
        "template": "recordatorio_cita",
        "language": "es",
        "variables": ["cliente.nombre", "cita.fecha"],
    }
    r = await app_client.put(url, headers=h, json={"mapping": {EVENTO: bueno}})
    assert r.status_code == 200, r.text
    r = await app_client.get(url, headers=h)
    assert "recordatorio_cita" in [t["name"] for t in r.json()["templates"]]

    contexto = {
        "cliente": {"nombre": "Ana"},
        "cita": {
            "titulo": "Corte",
            "fecha": "2026-11-20",
            "hora": "10:00",
            "zona": "America/Bogota",
            "enlace": None,
        },
    }
    async with tenant_scope(a["tid"]):
        resultado = await send_event_notice(
            a["conversacion"], EVENTO, contexto, notice_key=f"cita:{uuid.uuid4()}"
        )
    assert resultado.modo == "plantilla"
    [(comps, contenido)] = await _sql(
        a["tid"],
        "select template_components, content from messages where conversation_id = :c and sender_type = 'agent' "
        "and template_name = 'recordatorio_cita' order by created_at desc limit 1",
        c=a["conversacion"],
    )
    assert comps[0]["variables"] == {"1": "Ana", "2": "2026-11-20"}
    assert contenido == "Hola Ana, tu cita es el 2026-11-20"


async def test_plantilla_de_otra_conexion_del_mismo_negocio(negocios, app_client) -> None:
    """Dos conexiones de Twilio en el mismo negocio: la conversación de una no usa las plantillas de la otra."""
    from core.db.models import TwilioContentTemplate
    from core.db.session import get_session
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    ac = "AC" + uuid.uuid4().hex
    e164 = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    async with tenant_scope(a["tid"]):
        otra = await alta.conectar(
            a["tid"], account_sid=ac, auth_token="tok", sender_sid=XE, transport=_twilio(ac, e164)
        )
        async with get_session() as s:
            ajena = TwilioContentTemplate(
                tenant_id=a["tid"],
                channel_connection_id=otra.connection_id,
                content_sid="HX" + uuid.uuid4().hex,
                friendly_name="de_la_otra",
                whatsapp_name="de_la_otra",
                language="es",
                category="UTILITY",
                body="Hola {{1}}",
                types={"twilio/text": {"body": "Hola {{1}}"}},
                approval_status="approved",
            )
            s.add(ajena)
            await s.flush()
            ajena_id = ajena.id
    h = await api_headers(app_client, a["tid"])
    base = f"/api/v1/admin/conversations/{a['conversacion']}"
    nombres = [t["name"] for t in (await app_client.get(f"{base}/templates", headers=h)).json()]
    assert "de_la_otra" not in nombres
    r = await app_client.post(
        f"{base}/template-messages",
        headers=h,
        json={"template_id": str(ajena_id), "variables": {"1": "x"}},
    )
    assert r.status_code == 404

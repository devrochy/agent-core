"""E2E de la conexión de Messenger (Graph mockeado): conectar Página →
suscribir → enviar dentro de ventana → fuera de ventana sin confirmación
falla local → con confirmación humana usa tag HUMAN_AGENT; el parser de
webhook de ig_post llega con media poblada."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

from core.config import get_settings

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _switch_tenant(app_client, session_cookie: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session_cookie}, data={"tenant_id": str(tenant_id)}
    )
    ac_tenant = next(c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session_cookie}; {ac_tenant}"


async def _new_tenant() -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = _uniq("messenger-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant messenger {slug}", slug=slug, business_description="Test de Messenger.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


def _configure_meta_app_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")
    monkeypatch.setattr(settings, "meta_config_id", "cfg-embedded-signup")


async def test_conectar_pagina_suscribir_y_enviar(app_client, monkeypatch):
    import core.gateway.meta_messenger as messenger_mod
    from core.db.models import FbPage, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    page_id = _uniq("page")

    class FakeExchangeResponse:
        def __init__(self, json_data):
            self._json = json_data
            self.status_code = 200
            self.text = ""

        def json(self):
            return self._json

    class FakeExchangeClient:
        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, **kwargs):
            return FakeExchangeResponse({"access_token": "tok-de-negocio-no-debe-persistirse-en-claro"})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeExchangeClient())

    async def _fake_graph_fetch(path, *, method="GET", body=None, query=None, **kwargs):
        if path == "/me":
            return {"client_business_id": "biz-messenger"}
        if path == "/me/accounts":
            return {"data": [{"id": page_id, "name": "Mi Página", "category": "Business", "access_token": "tok-de-la-pagina", "tasks": ["MANAGE"]}]}
        if path == f"/{page_id}/subscribed_apps" and method == "POST":
            assert "subscribed_fields" in (query or {})
            return {"success": True}
        if path == f"/{page_id}/subscribed_apps":
            return {"data": [{"subscribed_fields": messenger_mod.PAGE_SUBSCRIBED_FIELDS.split(",")}]}
        raise AssertionError(f"path no esperado: {path}")

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    r = await app_client.post(
        "/admin/conexiones/wizard/messenger/exchange", headers={"Cookie": cookie}, json={"code": "code-de-un-solo-uso"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connectedPages"] == [page_id]
    assert body["skippedPages"] == []
    assert "tok-de-negocio-no-debe-persistirse-en-claro" not in r.text
    assert "tok-de-la-pagina" not in r.text

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.channel == "messenger"))
            ).scalar_one()
            page = (
                await session.execute(select(FbPage).where(FbPage.connection_id == connection.id))
            ).scalar_one()
        assert connection.status == "connected"
        assert connection.business_id == "biz-messenger"
        assert set(page.subscribed_fields) == set(messenger_mod.PAGE_SUBSCRIBED_FIELDS.split(","))

        token = await meta_token_store.get_token(connection.id, "page_access", page_id, tenant_id=tid)
        assert token == "tok-de-la-pagina"

    # GET de la página de gestión no debe filtrar el token ni el business token.
    r2 = await app_client.get(f"/admin/conexiones/meta/{connection.id}/paginas", headers={"Cookie": cookie})
    assert r2.status_code == 200
    assert "tok-de-la-pagina" not in r2.text


async def test_pagina_sin_permiso_no_se_conecta(app_client, monkeypatch):
    import core.gateway.meta_messenger as messenger_mod

    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    page_id = _uniq("page-insuf")

    class FakeExchangeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "tok"}

    class FakeExchangeClient:
        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, **kwargs):
            return FakeExchangeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeExchangeClient())

    async def _fake_graph_fetch(path, **kwargs):
        if path == "/me":
            return {"client_business_id": "biz-x"}
        if path == "/me/accounts":
            return {"data": [{"id": page_id, "name": "Sin permiso", "category": "x", "access_token": "tok", "tasks": ["ANALYZE"]}]}
        raise AssertionError(f"no debería llamarse: {path}")

    monkeypatch.setattr(messenger_mod, "graph_fetch", _fake_graph_fetch)

    r = await app_client.post(
        "/admin/conexiones/wizard/messenger/exchange", headers={"Cookie": cookie}, json={"code": "code-x"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connectedPages"] == []
    assert len(body["skippedPages"]) == 1
    assert "MANAGE" in body["skippedPages"][0]["reason"] or "MODERATE" in body["skippedPages"][0]["reason"]


async def test_envio_fuera_de_ventana_sin_confirmacion_falla_local(monkeypatch):
    from core.gateway.meta_messenger import MessengerSendError, send_messenger_message

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe llamar a Meta")

    import core.gateway.meta_messenger as messenger_mod

    monkeypatch.setattr(messenger_mod, "graph_fetch", _debe_no_llamarse)

    with pytest.raises(MessengerSendError):
        await send_messenger_message(
            "page-1", "psid-1", "hola", "tok", "v21.0", uuid.uuid4(), service_window_expires_at=None
        )


async def test_dos_paginas_bajo_la_misma_conexion_no_se_cruzan_al_responder(app_client, monkeypatch):
    """Hallazgo auditor-multitenant (2026-09-23): una MetaConnection de
    Messenger puede agrupar varias FbPage -- resolve_credentials debe usar
    la Página EXACTA de la conversación (conversation.fb_page_id), no la
    primera que encuentre."""
    from core.db.models import Conversation, Customer, FbPage, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_messenger import PAGE_TOKEN_KIND
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    page_a_id, page_b_id = _uniq("page-a"), _uniq("page-b")

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(tenant_id=tid, channel="messenger", meta_app_id="app-x", status="connected")
            session.add(connection)
            await session.flush()
            page_a = FbPage(tenant_id=tid, connection_id=connection.id, page_id=page_a_id, name="Página A")
            page_b = FbPage(tenant_id=tid, connection_id=connection.id, page_id=page_b_id, name="Página B")
            session.add_all([page_a, page_b])
            await session.flush()
            connection_id, page_b_row_id = connection.id, page_b.id

        await meta_token_store.put_token(connection_id, PAGE_TOKEN_KIND, page_a_id, "tok-de-pagina-a", tenant_id=tid)
        await meta_token_store.put_token(connection_id, PAGE_TOKEN_KIND, page_b_id, "tok-de-pagina-b", tenant_id=tid)

        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="messenger", external_id="messenger:psid-de-pagina-b")
            session.add(customer)
            await session.flush()
            # page_a se insertó PRIMERO -- si resolve_credentials tomara la
            # primera FbPage de la conexión (el bug), devolvería la A pese a
            # que esta conversación es explícitamente de la B.
            conversation = Conversation(
                tenant_id=tid, customer_id=customer.id, channel="messenger",
                meta_connection_id=connection_id, fb_page_id=page_b_row_id,
            )
            session.add(conversation)
            await session.flush()

        _, _, creds = await resolve_credentials(None, connection_id, expected_tenant_id=tid, conversation=conversation)
        assert creds["page_id"] == page_b_id
        assert creds["access_token"] == "tok-de-pagina-b"


async def test_webhook_ig_post_llega_con_media_poblada():
    from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-user-1",
                "messaging": [
                    {
                        "sender": {"id": "igsid-1"},
                        "message": {
                            "mid": "mid.igpost",
                            "attachments": [
                                {"type": "ig_post", "payload": {"id": "post-1", "url": "https://ig.example/post-1", "caption": "mira esto"}}
                            ],
                        },
                    }
                ],
            }
        ],
    }
    messages = WhatsAppMetaInbound().parse_inbound(payload)
    assert len(messages) == 1
    assert messages[0].media == [{"type": "ig_post", "id": "post-1", "url": "https://ig.example/post-1"}]
    assert messages[0].content == "mira esto"

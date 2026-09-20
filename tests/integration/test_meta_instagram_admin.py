"""E2E de la conexión de Instagram: ruta (a) vía Facebook Login (Graph
mockeado, graph.facebook.com), ruta (b) vía Instagram Login (httpx
mockeado, api.instagram.com/graph.instagram.com), y dos IgAccount bajo la
misma conexión que no se cruzan al responder (mismo patrón que
conversations.fb_page_id, ADR-0031)."""

from __future__ import annotations

import os
import uuid

import httpx
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

    slug = _uniq("instagram-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant instagram {slug}", slug=slug, business_description="Test de Instagram.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


def _configure_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "fb-app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "fb-s3cr3t")
    monkeypatch.setattr(settings, "meta_config_id_instagram", "cfg-instagram-dedicado")
    monkeypatch.setattr(settings, "instagram_app_id", "ig-app-999")
    monkeypatch.setattr(settings, "instagram_app_secret", "ig-s3cr3t")


class _FakeHttpxResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = ""
        self.headers = {}

    def json(self):
        return self._json


async def test_ruta_a_facebook_login_conecta_pagina_y_cuenta(app_client, monkeypatch):
    import core.gateway.meta_instagram as ig_mod
    from core.db.models import FbPage, IgAccount, MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _configure_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    page_id, ig_user_id = _uniq("page"), _uniq("iguser")

    class FakeExchangeClient:
        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, **kwargs):
            return _FakeHttpxResponse({"access_token": "tok-de-negocio-no-debe-persistirse"})

    monkeypatch.setattr(httpx, "AsyncClient", FakeExchangeClient())

    async def _fake_graph_fetch(path, **kwargs):
        if path == "/me/accounts":
            return {
                "data": [
                    {
                        "id": page_id, "name": "Mi Página", "category": "Business", "access_token": "tok-de-la-pagina",
                        "tasks": ["MANAGE"], "instagram_business_account": {"id": ig_user_id, "username": "mi_negocio"},
                    }
                ]
            }
        raise AssertionError(f"path no esperado: {path}")

    monkeypatch.setattr(ig_mod, "graph_fetch", _fake_graph_fetch)

    r = await app_client.post(
        "/admin/conexiones/wizard/instagram/exchange", headers={"Cookie": cookie}, json={"code": "code-de-un-solo-uso"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connectedAccounts"] == [ig_user_id]
    assert "tok-de-negocio-no-debe-persistirse" not in r.text
    assert "tok-de-la-pagina" not in r.text

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.channel == "instagram"))
            ).scalar_one()
            account = (
                await session.execute(select(IgAccount).where(IgAccount.connection_id == connection.id))
            ).scalar_one()
            page = (
                await session.execute(select(FbPage).where(FbPage.connection_id == connection.id))
            ).scalar_one()
        assert account.login_route == "ig_facebook_login"
        assert account.linked_page_id == page_id
        assert page.page_id == page_id


async def test_ruta_b_instagram_login_completa_el_canje(app_client, monkeypatch):
    import core.gateway.meta_instagram as ig_mod
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    _configure_settings(monkeypatch)
    tid = await _new_tenant()

    ig_user_id = _uniq("iguser-b")

    async def _fake_post(url, data=None, **kwargs):
        assert url == ig_mod.IG_TOKEN_EXCHANGE_URL
        assert data["client_id"] == "ig-app-999"
        return _FakeHttpxResponse({"access_token": "tok-corto"})

    async def _fake_get(url, params=None, **kwargs):
        if url == f"{ig_mod.IG_GRAPH_BASE}/access_token":
            assert params["client_secret"] == "ig-s3cr3t"
            return _FakeHttpxResponse({"access_token": "tok-largo-60-dias"})
        if url == f"{ig_mod.IG_GRAPH_BASE}/me":
            return _FakeHttpxResponse({"id": ig_user_id, "username": "otra_cuenta"})
        raise AssertionError(f"url no esperada: {url}")

    class FakeClient:
        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kwargs):
            return await _fake_post(url, data, **kwargs)

        async def get(self, url, params=None, **kwargs):
            return await _fake_get(url, params, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient())

    async with tenant_scope(tid):
        account = await ig_mod.connect_instagram_login(tid, "code-b", "https://panel.example.test/callback")
    assert account.login_route == "ig_instagram_login"
    assert account.ig_user_id == ig_user_id

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.channel == "instagram"))
            ).scalar_one()
        token = await meta_token_store.get_token(connection.id, ig_mod.IG_LONG_LIVED_TOKEN_KIND, ig_user_id, tenant_id=tid)
    assert token == "tok-largo-60-dias"


async def test_dos_cuentas_bajo_la_misma_conexion_no_se_cruzan_al_responder(app_client, monkeypatch):
    from core.db.models import Conversation, Customer, IgAccount, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_instagram import IG_LONG_LIVED_TOKEN_KIND
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    ig_a, ig_b = _uniq("ig-a"), _uniq("ig-b")

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(tenant_id=tid, channel="instagram", meta_app_id="ig-app-x", status="connected")
            session.add(connection)
            await session.flush()
            account_a = IgAccount(tenant_id=tid, connection_id=connection.id, ig_user_id=ig_a, login_route="ig_instagram_login")
            account_b = IgAccount(tenant_id=tid, connection_id=connection.id, ig_user_id=ig_b, login_route="ig_instagram_login")
            session.add_all([account_a, account_b])
            await session.flush()
            connection_id, account_b_row_id = connection.id, account_b.id

        await meta_token_store.put_token(connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_a, "tok-cuenta-a", tenant_id=tid)
        await meta_token_store.put_token(connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_b, "tok-cuenta-b", tenant_id=tid)

        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="instagram", external_id=f"instagram:{ig_b}:igsid-x")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=tid, customer_id=customer.id, channel="instagram",
                meta_connection_id=connection_id, ig_account_id=account_b_row_id,
            )
            session.add(conversation)
            await session.flush()

        _, _, creds = await resolve_credentials(None, connection_id, expected_tenant_id=tid, conversation=conversation)
        assert creds["ig_user_id"] == ig_b
        assert creds["access_token"] == "tok-cuenta-b"

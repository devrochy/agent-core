"""E2E del wizard de Embedded Signup (Graph API mockeada): reanudación tras
"cerrar el navegador" sin repetir el OAuth, coexistence nunca llama a
/register, y el puente de envío saliente (ADR-0030) funciona sin
ChannelConnection."""

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

    slug = _uniq("wizard-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant wizard {slug}", slug=slug, business_description="Test del wizard.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


def _configure_meta_app_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")
    monkeypatch.setattr(settings, "meta_config_id", "cfg-embedded-signup")


class FakeExchangeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json


class FakeExchangeClient:
    def __init__(self, token="tok-de-prueba-no-debe-persistirse-en-claro"):
        self._token = token

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kwargs):
        return FakeExchangeResponse(json_data={"access_token": self._token})


def _fake_graph_fetch(register_calls: list, waba_id: str, phone_number_id: str):
    async def _inner(path, *, method="GET", body=None, **kwargs):
        if path == f"/{waba_id}":
            return {"name": "Mi WABA", "currency": "USD", "timezone_id": "1", "message_template_namespace": "ns", "account_review_status": "APPROVED"}
        if path == f"/{phone_number_id}":
            return {"display_phone_number": "+1 555 0100", "verified_name": "Mi Negocio", "quality_rating": "GREEN"}
        if path == f"/{phone_number_id}/register":
            register_calls.append(body)
            return {"success": True}
        if path.endswith("/message_templates"):
            return {"data": []}
        raise AssertionError(f"path no esperado en graph_fetch: {path}")

    return _inner


async def _noop_upsert_subscription(*a, **k):
    class _Status:
        active = True

    return _Status()


async def _fake_list_app_subscriptions(meta_app_id=None):
    class _Status:
        object = "whatsapp_business_account"
        active = True

    return [_Status()]


async def test_reanuda_tras_cerrar_el_navegador_sin_repetir_oauth(app_client, monkeypatch):
    import core.gateway.meta_onboarding as onboarding_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.db.models import ConnectionWizard, MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    # Paso 1: primer GET crea el wizard en select_channel (sin conexión Meta previa).
    r1 = await app_client.get("/admin/conexiones/wizard/whatsapp", headers={"Cookie": cookie})
    assert r1.status_code == 200
    async with tenant_scope(tid):
        async with get_session() as session:
            wizard = (await session.execute(select(ConnectionWizard).where(ConnectionWizard.tenant_id == tid))).scalar_one()
    assert wizard.current_step == "select_channel"
    wizard_id = wizard.id

    # Paso 2: el popup termina con FINISH -> exchange inmediato.
    waba_id, phone_number_id = _uniq("waba"), _uniq("phone")
    monkeypatch.setattr(httpx, "AsyncClient", FakeExchangeClient())
    register_calls: list = []
    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch(register_calls, waba_id, phone_number_id))
    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _noop_upsert_subscription)
    monkeypatch.setattr(subs_mod, "list_app_subscriptions", _fake_list_app_subscriptions)

    r2 = await app_client.post(
        "/admin/conexiones/wizard/whatsapp/exchange",
        headers={"Cookie": cookie},
        json={
            "wizard_id": str(wizard_id), "code": "code-de-un-solo-uso", "event_type": "FINISH",
            "waba_id": waba_id, "phone_number_id": phone_number_id, "business_id": "biz-123",
        },
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert set(body) == {"connectionId", "nextStep"}, "la respuesta no debe incluir el token"
    connection_id = uuid.UUID(body["connectionId"])
    assert body["nextStep"] == "discover_assets"

    # "Cerrar el navegador": nueva request sin ningún estado de cliente.
    r3 = await app_client.get("/admin/conexiones/wizard/whatsapp", headers={"Cookie": cookie})
    assert r3.status_code == 200
    assert "registrar-numero" in r3.text  # llegó hasta el paso de PIN, sin repetir OAuth

    async with tenant_scope(tid):
        async with get_session() as session:
            wizard = await session.get(ConnectionWizard, wizard_id)
            waba_row = (await session.execute(select(WabaAccount).where(WabaAccount.connection_id == connection_id))).scalar_one()
            phone_row = (await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == connection_id))).scalar_one()
    assert wizard.current_step == "register_number"
    assert waba_row.name == "Mi WABA"
    assert phone_row.verified_name == "Mi Negocio"
    assert register_calls == [], "no debe llamar a /register todavía"

    # Paso 3: PIN -> completa el resto de la cadena automática.
    r4 = await app_client.post(
        "/admin/conexiones/wizard/whatsapp/registrar-numero",
        headers={"Cookie": cookie}, data={"wizard_id": str(wizard_id), "pin": "123456"},
    )
    assert r4.status_code == 303
    assert register_calls == [{"messaging_product": "whatsapp", "pin": "123456"}]

    async with tenant_scope(tid):
        async with get_session() as session:
            wizard = await session.get(ConnectionWizard, wizard_id)
            connection = await session.get(MetaConnection, connection_id)
    assert wizard.current_step == "done"
    assert connection.status == "connected"


async def test_coexistence_nunca_llama_a_register(app_client, monkeypatch):
    import core.gateway.meta_onboarding as onboarding_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.db.models import ConnectionWizard, MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    await app_client.get("/admin/conexiones/wizard/whatsapp", headers={"Cookie": cookie})
    async with tenant_scope(tid):
        async with get_session() as session:
            wizard = (await session.execute(select(ConnectionWizard).where(ConnectionWizard.tenant_id == tid))).scalar_one()

    waba_id = _uniq("waba-coex")
    monkeypatch.setattr(httpx, "AsyncClient", FakeExchangeClient())
    register_calls: list = []

    async def _fail_if_register_called(path, *, method="GET", body=None, **kwargs):
        if path.endswith("/register"):
            raise AssertionError("coexistence NUNCA debe llamar a /register")
        if path == f"/{waba_id}":
            return {"name": "WABA coexistence"}
        if path.endswith("/message_templates"):
            return {"data": []}
        raise AssertionError(f"path no esperado: {path}")

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fail_if_register_called)
    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _noop_upsert_subscription)
    monkeypatch.setattr(subs_mod, "list_app_subscriptions", _fake_list_app_subscriptions)

    r = await app_client.post(
        "/admin/conexiones/wizard/whatsapp/exchange", headers={"Cookie": cookie},
        json={
            "wizard_id": str(wizard.id), "code": "code-coex", "event_type": "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
            "waba_id": waba_id, "business_id": "biz-coex",
        },
    )
    assert r.status_code == 200, r.text

    r2 = await app_client.get("/admin/conexiones/wizard/whatsapp", headers={"Cookie": cookie})
    assert r2.status_code == 200
    assert register_calls == []

    async with tenant_scope(tid):
        async with get_session() as session:
            wizard = await session.get(ConnectionWizard, wizard.id)
            connection = await session.get(MetaConnection, wizard.connection_id)
    assert "register_number" in wizard.completed_steps
    assert wizard.current_step == "done"
    assert connection.health.get("coexistence") is True


async def test_envio_saliente_usa_el_puente_sin_channel_connection(app_client, monkeypatch):
    """ADR-0030: una conexión hecha por el wizard (sin ChannelConnection)
    debe poder enviar mensajes a través del puente meta_connection_id."""
    from core.db.models import Conversation, Customer, MetaConnection, PhoneNumber
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.channels.whatsapp import outbound as outbound_mod
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    phone_number_id = _uniq("bridge-phone")

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(tenant_id=tid, channel="whatsapp", meta_app_id="app-x", status="connected", business_id="biz-bridge")
            session.add(connection)
            await session.flush()
            session.add(PhoneNumber(tenant_id=tid, connection_id=connection.id, phone_number_id=phone_number_id))
            connection_id = connection.id

        await meta_token_store.put_token(connection_id, TOKEN_KIND, "biz-bridge", "tok-envio-real", tenant_id=tid)

        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id="whatsapp:+15550000000")
            session.add(customer)
            await session.flush()
            conversation = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp", meta_connection_id=connection_id)
            session.add(conversation)
            await session.flush()

        provider, creds = await resolve_credentials(None, connection_id, expected_tenant_id=tid)
        assert provider == "meta"
        assert creds == {"phone_number_id": phone_number_id, "access_token": "tok-envio-real"}

        # Hallazgo auditor-multitenant (2026-09-22): si el tenant esperado no
        # coincide con el dueño real de la MetaConnection, nunca debe
        # devolver el token -- cae al fallback (.env, sin credenciales reales).
        _, creds_mismatch = await resolve_credentials(None, connection_id, expected_tenant_id=uuid.uuid4())
        assert creds_mismatch.get("access_token") != "tok-envio-real"

        sent = {}

        class _FakeHttpResponse:
            status_code = 200

            def json(self):
                return {"messages": [{"id": "wamid.enviado"}]}

        class _FakeHttpClient:
            def __call__(self, *a, **k):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                sent["url"] = url
                sent["headers"] = headers
                return _FakeHttpResponse()

        monkeypatch.setattr(outbound_mod.httpx, "AsyncClient", _FakeHttpClient())

        message_id = await outbound_mod.WhatsAppMetaOutbound().send_text(creds, customer.external_id, "hola")
        assert message_id == "wamid.enviado"
        assert phone_number_id in sent["url"]
        assert sent["headers"]["Authorization"] == "Bearer tok-envio-real"

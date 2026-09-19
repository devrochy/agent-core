"""Rutas admin de suscripciones de webhook a nivel de app
(`/admin/conexiones/webhooks*`): gate owner/master, y que la respuesta JSON
nunca exponga `app_secret`/`verify_token`."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _login(app_client, email: str, password: str) -> str:
    r = await app_client.post("/admin/login", data={"email": email, "password": password})
    assert r.status_code == 303, f"login falló para {email}: {r.text}"
    return r.headers["set-cookie"].split(";")[0]


async def _switch_tenant(app_client, session_cookie: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session_cookie}, data={"tenant_id": str(tenant_id)}
    )
    ac_tenant = next(c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session_cookie}; {ac_tenant}"


async def _make_tenant_with_connection(channel: str = "whatsapp") -> uuid.UUID:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = _uniq("subs-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant suscripciones {slug}",
        slug=slug,
        business_description="Test de rutas admin de suscripciones.",
        persona={"tone": "cercano"},
        admin_email="admin@local",
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                MetaConnection(
                    tenant_id=tid,
                    channel=channel,
                    meta_app_id=f"app-{slug}",
                    status="connected",
                )
            )
            await session.commit()
    return tid


async def test_json_requiere_sesion(app_client):
    r = await app_client.get("/admin/conexiones/webhooks.json")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


async def test_registrar_devuelve_403_sin_rol_owner_o_master(app_client):
    """Un admin sin `TenantUser.role == 'owner'` y sin `Admin.role == 'master'`
    no puede re-registrar -- criterio de aceptación implícito del ticket
    (gate "solo owner/admin"), traducido a los roles reales de este repo."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _make_tenant_with_connection()

    colaborador_email = f"{_uniq('colaborador')}@example.test"
    async with get_session() as session:
        colaborador = Admin(email=colaborador_email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(colaborador)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=colaborador.id, role="colaborador"))
        await session.commit()

    session_cookie = await _login(app_client, colaborador_email, "clave-segura-123")
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.post(
        "/admin/conexiones/webhooks/registrar", headers={"Cookie": cookie}, data={"channel": "whatsapp"}
    )
    assert r.status_code == 403


async def test_registrar_permite_a_owner_del_tenant(app_client, monkeypatch):
    """Un `TenantUser.role == 'owner'` (sin ser `master`) SÍ puede re-registrar
    -- primer uso real de este rol en una ruta."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _make_tenant_with_connection()

    owner_email = f"{_uniq('owner')}@example.test"
    async with get_session() as session:
        owner = Admin(email=owner_email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(owner)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=owner.id, role="owner"))
        await session.commit()

    import core.gateway.meta_preflight as preflight_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.gateway.meta_preflight import AppCredentials
    from core.gateway.meta_subscriptions import AppSubscriptionStatus

    async def _fake_resolve(tenant_id, channel="whatsapp"):
        return AppCredentials(app_id="app-fake", app_secret="s3cr3t-no-debe-salir", config_id="", graph_api_version="v21.0")

    async def _fake_upsert(config, object_, *, callback_url, verify_token):
        return AppSubscriptionStatus(
            meta_app_id=config.app_id, object=object_, callback_url=callback_url, active=True,
            fields=["messages"], last_checked_at=None, last_error=None,
        )

    monkeypatch.setattr(preflight_mod, "resolve_app_credentials", _fake_resolve)
    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _fake_upsert)

    session_cookie = await _login(app_client, owner_email, "clave-segura-123")
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.post(
        "/admin/conexiones/webhooks/registrar", headers={"Cookie": cookie}, data={"channel": "whatsapp"}
    )
    assert r.status_code == 303
    assert "danger" not in r.headers["location"]
    assert "s3cr3t-no-debe-salir" not in r.headers["location"]


async def test_json_no_expone_secretos(app_client):
    from core.db.models import MetaAppSubscription, MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _make_tenant_with_connection()

    async with tenant_scope(tid):
        async with get_session() as session:
            conn = (
                await session.execute(select(MetaConnection).where(MetaConnection.tenant_id == tid))
            ).scalar_one()

    async with get_session() as session:
        session.add(
            MetaAppSubscription(
                meta_app_id=conn.meta_app_id,
                object="whatsapp_business_account",
                callback_url="https://example.test/webhooks/whatsapp",
                active=True,
                fields=["messages"],
            )
        )
        await session.commit()

    session_cookie = await _login(app_client, "admin@local", "admin123")
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get("/admin/conexiones/webhooks.json", headers={"Cookie": cookie})
    assert r.status_code == 200
    body = r.text
    assert "app_secret" not in body
    assert "verify_token" not in body
    data = r.json()
    assert data["subscriptions"][0]["callback_url"] == "https://example.test/webhooks/whatsapp"
    assert data["subscriptions"][0]["active"] is True

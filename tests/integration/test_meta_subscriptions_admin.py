"""Suscripciones de webhook a nivel de app en la API nueva
(`/api/v1/admin/meta/webhooks*`): exige sesión, registrar exige owner/master, y
la respuesta JSON nunca expone `app_secret`/`verify_token`."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _make_tenant_with_connection(channel: str = "whatsapp") -> uuid.UUID:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("subs-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant suscripciones {slug}",
        slug=slug,
        business_description="Test de rutas admin de suscripciones.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                MetaConnection(
                    tenant_id=tid,
                    channel=channel,
                    origin="byok", meta_app_id=f"app-{slug}",
                    status="connected",
                )
            )
            await session.commit()
    return tid


async def test_listado_requiere_sesion(app_client) -> None:
    r = await app_client.get("/api/v1/admin/meta/webhooks")
    assert r.status_code == 401


async def test_registrar_devuelve_403_sin_rol_owner_o_master(app_client) -> None:
    """Un admin sin `TenantUser.role == 'owner'` y sin `Admin.role == 'master'`
    no puede re-registrar -- gate "solo owner/admin" del endpoint."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _make_tenant_with_connection()

    colaborador_email = f"{uniq('colaborador')}@example.test"
    async with get_session() as session:
        colaborador = Admin(
            email=colaborador_email,
            password_hash=hash_password("clave-segura-123"),
            role="colaborador",
        )
        session.add(colaborador)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=colaborador.id, role="colaborador"))
        await session.commit()

    headers = await api_headers(app_client, tid, email=colaborador_email, password="clave-segura-123")
    r = await app_client.post(
        "/api/v1/admin/meta/webhooks/register",
        json={"channel": "whatsapp"},
        headers=headers,
    )
    assert r.status_code == 403


async def test_registrar_permite_a_owner_del_tenant(app_client, monkeypatch) -> None:
    """Un `TenantUser.role == 'owner'` (sin ser `master`) SÍ puede re-registrar."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _make_tenant_with_connection()

    owner_email = f"{uniq('owner')}@example.test"
    async with get_session() as session:
        owner = Admin(
            email=owner_email, password_hash=hash_password("clave-segura-123"), role="colaborador"
        )
        session.add(owner)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=owner.id, role="owner"))
        await session.commit()

    import core.gateway.meta_preflight as preflight_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.gateway.meta_preflight import AppCredentials
    from core.gateway.meta_subscriptions import AppSubscriptionStatus

    async def _fake_resolve(tenant_id, channel="whatsapp"):
        return AppCredentials(
            app_id="app-fake",
            app_secret="s3cr3t-no-debe-salir",
            config_id="",
            graph_api_version="v21.0",
        )

    async def _fake_upsert(config, object_, *, callback_url, verify_token):
        return AppSubscriptionStatus(
            meta_app_id=config.app_id,
            object=object_,
            callback_url=callback_url,
            active=True,
            fields=["messages"],
            last_checked_at=None,
            last_error=None,
        )

    monkeypatch.setattr(preflight_mod, "resolve_app_credentials", _fake_resolve)
    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _fake_upsert)

    headers = await api_headers(app_client, tid, email=owner_email, password="clave-segura-123")
    r = await app_client.post(
        "/api/v1/admin/meta/webhooks/register",
        json={"channel": "whatsapp"},
        headers=headers,
    )
    assert r.status_code == 204, r.text


async def test_listado_no_expone_secretos(app_client) -> None:
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

    headers = await api_headers(app_client, tid)
    r = await app_client.get("/api/v1/admin/meta/webhooks", headers=headers)
    assert r.status_code == 200
    body = r.text
    assert "app_secret" not in body
    assert "verify_token" not in body
    data = r.json()
    assert data["subscriptions"][0]["callback_url"] == "https://example.test/webhooks/whatsapp"
    assert data["subscriptions"][0]["active"] is True

"""Criterio de aceptación explícito: desuscribir manualmente la app de una
WABA (mock) hace que el check pase a rojo en menos de una hora (acá: en el
próximo diagnóstico on-demand) y el botón de reparación la vuelve a
suscribir. También cronometra que el diagnóstico on-demand corre en <10s
para 1 WABA + 2 números."""

from __future__ import annotations

import os
import time
import uuid

import pytest

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


async def _setup_connected_tenant():
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = _uniq("salud-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant salud {slug}", slug=slug, business_description="Test de salud de conexiones.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    waba_id = _uniq("waba")
    phone_ids = [_uniq("phone"), _uniq("phone")]

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", meta_app_id="app-embedded-signup",
                status="connected", business_id="biz-salud",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
            session.add(waba)
            await session.flush()
            for phone_id in phone_ids:
                session.add(
                    PhoneNumber(tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id, phone_number_id=phone_id)
                )
            connection_id = connection.id

        await meta_token_store.put_token(connection_id, TOKEN_KIND, "biz-salud", "tok-salud", tenant_id=tid)
        await meta_token_store.put_token(connection_id, "app_access", "app-embedded-signup", "s3cr3t-de-la-app", tenant_id=tid)
    return tid, connection_id, waba_id, phone_ids


def _configure_meta_app_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")


async def test_desuscribir_waba_pasa_a_rojo_y_el_boton_repara(app_client, monkeypatch):
    import core.gateway.meta_health as health_mod
    import core.gateway.meta_onboarding as onboarding_mod

    _configure_meta_app_settings(monkeypatch)
    tid, connection_id, _waba_id, phone_ids = await _setup_connected_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    subscribed = {"value": False}  # WABA desuscripta -- simula la revocación manual

    async def _fake_graph_fetch(path, *, method="GET", query=None, body=None, **kwargs):
        if path.endswith("/subscribed_apps") and method == "POST":
            subscribed["value"] = True
            return {"success": True}
        if path.endswith("/subscribed_apps"):
            return {"data": [{"whatsapp_business_api_data": {}}]} if subscribed["value"] else {"data": []}
        if path == f"/{phone_ids[0]}" or path == f"/{phone_ids[1]}":
            if query and query.get("fields") == "webhook_configuration":
                return {"webhook_configuration": {}}
            return {"quality_rating": "GREEN", "code_verification_status": "VERIFIED"}
        if path == "/debug_token":
            return {"data": {"is_valid": True, "expires_at": 0}}
        raise AssertionError(f"path no esperado: {method} {path}")

    async def _fake_debug_token(token, app_id, app_secret, version):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="token_valid", ok=True, detail="ok", checked_at=datetime.now(UTC))

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr(health_mod, "check_token_valid", _fake_debug_token)
    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    async def _fake_list_app_subscriptions(meta_app_id):
        return []  # "todavía no corrió" -- no debe afectar el semáforo por sí solo

    monkeypatch.setattr("core.gateway.meta_subscriptions.list_app_subscriptions", _fake_list_app_subscriptions)

    # 1. Primer diagnóstico -- WABA desuscripta, debe dar rojo/degradado.
    started = time.monotonic()
    r1 = await app_client.post(f"/admin/conexiones/salud/{connection_id}/diagnosticar", headers={"Cookie": cookie})
    elapsed = time.monotonic() - started
    assert r1.status_code == 303
    assert elapsed < 10.0, f"el diagnóstico on-demand tardó {elapsed:.1f}s (>10s)"

    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = await session.get(MetaConnection, connection_id)
    assert connection.health["webhook_subscribed"]["ok"] is False
    assert connection.health["webhook_subscribed"]["repair_action"] == "subscribe_waba"
    assert connection.status in ("degraded", "error")

    # 2. Reparación: el botón re-suscribe SOLO ese paso.
    r2 = await app_client.post(
        f"/admin/conexiones/salud/{connection_id}/resubscribe-waba", headers={"Cookie": cookie}
    )
    assert r2.status_code == 303
    assert "danger" not in r2.headers["location"], r2.headers["location"]
    assert subscribed["value"] is True

    # 3. Segundo diagnóstico -- ahora debe dar verde.
    r3 = await app_client.post(f"/admin/conexiones/salud/{connection_id}/diagnosticar", headers={"Cookie": cookie})
    assert r3.status_code == 303

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = await session.get(MetaConnection, connection_id)
    assert connection.health["webhook_subscribed"]["ok"] is True

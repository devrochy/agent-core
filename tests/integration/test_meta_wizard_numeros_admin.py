"""Panel de números adicionales (`/admin/conexiones/meta/{connection_id}/
numeros`): agregar número, solicitar código, verificar, rate limit de
reenvío contra Redis real."""

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


async def _setup_connected_tenant():
    """Tenant con MetaConnection connected + WabaAccount + token guardado --
    salta el wizard, va directo al escenario de "ya conectado, gestionando números"."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.shared.tenant import tenant_scope

    slug = _uniq("numeros-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant números {slug}", slug=slug, business_description="Test de números adicionales.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    waba_id = _uniq("waba")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin="embedded_signup", meta_app_id="app-embedded-signup",
                status="connected", business_id="biz-numeros",
            )
            session.add(connection)
            await session.flush()
            session.add(WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id))
            connection_id = connection.id

        await meta_token_store.put_token(connection_id, TOKEN_KIND, "biz-numeros", "tok-numeros", tenant_id=tid)
    return tid, connection_id, waba_id


def _configure_meta_app_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")


async def test_agregar_solicitar_codigo_y_verificar(app_client, monkeypatch):
    import core.gateway.meta_phone_numbers as phone_numbers_mod

    _configure_meta_app_settings(monkeypatch)
    tid, connection_id, waba_id = await _setup_connected_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    added_phone_id = _uniq("phone")

    async def _fake_graph_fetch(path, *, method="GET", body=None, **kwargs):
        if path == f"/{waba_id}/phone_numbers" and method == "POST":
            return {"id": added_phone_id}
        if path == f"/{waba_id}/phone_numbers":
            return {"data": [], "paging": {"cursors": {}}}
        if path == f"/{added_phone_id}/request_code":
            assert body == {"code_method": "SMS", "language": "es"}
            return {"success": True}
        if path == f"/{added_phone_id}/verify_code":
            assert body == {"code": "246810"}
            return {"success": True}
        raise AssertionError(f"path no esperado: {path}")

    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", _fake_graph_fetch)

    r1 = await app_client.get(f"/admin/conexiones/meta/{connection_id}/numeros", headers={"Cookie": cookie})
    assert r1.status_code == 200

    r2 = await app_client.post(
        f"/admin/conexiones/meta/{connection_id}/numeros/agregar", headers={"Cookie": cookie},
        data={"cc": "57", "phone_number": "3001234567", "verified_name": "Mi Negocio"},
    )
    assert r2.status_code == 303
    assert "danger" not in r2.headers["location"]

    r3 = await app_client.post(
        f"/admin/conexiones/meta/{connection_id}/numeros/{added_phone_id}/solicitar-codigo",
        headers={"Cookie": cookie}, data={"code_method": "SMS", "language": "es"},
    )
    assert r3.status_code == 303
    assert "danger" not in r3.headers["location"]

    # Reenviar antes de que expire el rate limit -> rechazado, sin llamar a Meta de nuevo.
    r3b = await app_client.post(
        f"/admin/conexiones/meta/{connection_id}/numeros/{added_phone_id}/solicitar-codigo",
        headers={"Cookie": cookie}, data={"code_method": "SMS", "language": "es"},
    )
    assert "danger" in r3b.headers["location"]

    r4 = await app_client.post(
        f"/admin/conexiones/meta/{connection_id}/numeros/{added_phone_id}/verificar",
        headers={"Cookie": cookie}, data={"code": "246810"},
    )
    assert r4.status_code == 303
    assert "danger" not in r4.headers["location"]

    from core.db.models import PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            row = (
                await session.execute(select(PhoneNumber).where(PhoneNumber.phone_number_id == added_phone_id))
            ).scalar_one()
    assert row.code_verification_status == "VERIFIED"

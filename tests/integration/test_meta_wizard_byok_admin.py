"""Criterio de aceptación: el modal BYOK aparece al elegir "Modo prueba",
POST .../guardar con Graph mockeada crea la MetaConnection y redirige a
/admin/conexiones/salud, y credenciales rechazadas no persisten nada."""

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

    slug = _uniq("wizard-byok")
    tid, _ = await onboard_tenant(
        name=f"Tenant wizard byok {slug}", slug=slug, business_description="Test del wizard BYOK.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


async def test_elegir_entorno_muestra_el_modal_byok(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get("/admin/conexiones/wizard/whatsapp/elegir-entorno", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Conectar WhatsApp de prueba" in r.text
    assert 'action="/admin/conexiones/wizard/whatsapp-byok/guardar"' in r.text
    assert "App ID" in r.text
    assert "Phone Number ID" in r.text


async def test_guardar_byok_con_credenciales_validas_crea_conexion_y_redirige_a_salud(app_client, monkeypatch):
    import core.gateway.meta_byok as byok_mod
    from core.db.models import MetaConnection, PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    async def _fake_verify(phone_number_id, access_token, app_secret):
        return True, "ok", {"display_phone_number": "+1 555 0100", "verified_name": "Mi Negocio Test"}

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _fake_verify)

    app_id = _uniq("app-byok")
    phone_number_id = _uniq("phone-byok")
    r = await app_client.post(
        "/admin/conexiones/wizard/whatsapp-byok/guardar",
        headers={"Cookie": cookie},
        data={"app_id": app_id, "app_secret": "secret-byok", "phone_number_id": phone_number_id, "access_token": "tok-byok"},
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/conexiones/salud?flash=")

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(
                    select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.meta_app_id == app_id)
                )
            ).scalar_one()
            phone = (
                await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == connection.id))
            ).scalar_one()

    assert connection.status == "connected"
    assert connection.environment == "sandbox"
    assert phone.phone_number_id == phone_number_id
    assert phone.registered is True


async def test_guardar_byok_con_credenciales_invalidas_no_crea_nada(app_client, monkeypatch):
    import core.gateway.meta_byok as byok_mod
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    async def _fake_verify_fail(phone_number_id, access_token, app_secret):
        return False, "Meta rechazó las credenciales: token inválido", {}

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _fake_verify_fail)

    r = await app_client.post(
        "/admin/conexiones/wizard/whatsapp-byok/guardar",
        headers={"Cookie": cookie},
        data={"app_id": "app-byok-2", "app_secret": "secret-x", "phone_number_id": "phone-x", "access_token": "tok-x"},
    )
    assert r.status_code == 303
    assert "elegir-entorno" in r.headers["location"]
    assert "flash_type=danger" in r.headers["location"]

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(
                    select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.meta_app_id == "app-byok-2")
                )
            ).scalar_one_or_none()
    assert connection is None

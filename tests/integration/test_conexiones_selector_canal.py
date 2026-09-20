"""Criterio de aceptación: el modal "Agregar conexión" de /admin/conexiones
muestra un grid con las 5 opciones de canal (WhatsApp, Instagram, Messenger,
WhatsApp vía Twilio, WhatsApp vía Plivo); solo WhatsApp (Meta) es clickeable
y lleva al wizard real, el resto queda deshabilitado ("Próximamente")."""

from __future__ import annotations

import os
import uuid

import pytest

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


async def _new_tenant():
    from core.onboarding import onboard_tenant

    slug = _uniq("selector-canal")
    tid, _ = await onboard_tenant(
        name=f"Tenant selector canal {slug}", slug=slug, business_description="Test del selector de canal.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


async def test_modal_muestra_las_cinco_opciones_de_canal(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get("/admin/conexiones", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Elegí el canal a conectar" in r.text
    assert "WhatsApp" in r.text
    assert "Instagram" in r.text
    assert "Messenger" in r.text
    assert "vía Twilio" in r.text
    assert "vía Plivo" in r.text
    assert r.text.count("Próximamente") == 4


async def test_solo_whatsapp_meta_es_clickeable_hacia_el_wizard(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get("/admin/conexiones", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert 'href="/admin/conexiones/wizard/whatsapp"' in r.text
    assert r.text.count('aria-disabled="true"') == 4

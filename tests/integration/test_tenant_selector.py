"""Selector de tenant del panel (C3, Sprint 4): la cookie ac_tenant decide el
tenant activo y SOLO permite tenants del admin (sin escalada de acceso)."""

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


async def _login(app_client) -> str:
    """Login y devuelve la cookie de sesión."""
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].split(";")[0]
    return cookie


async def test_switch_tenant_valida_acceso(app_client, moto_setup) -> None:
    _t1, t2 = moto_setup
    cookie = await _login(app_client)
    headers = {"Cookie": cookie}

    # tenant del admin → 303 + cookie ac_tenant set
    r = await app_client.post(
        "/admin/switch-tenant", headers=headers, data={"tenant_id": str(t2)}
    )
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "ac_tenant" in set_cookie, "debe fijar la cookie ac_tenant"

    # tenant ajeno (UUID aleatorio) → rechazado, sin cookie, flash de error
    r2 = await app_client.post(
        "/admin/switch-tenant", headers=headers, data={"tenant_id": str(uuid.uuid4())}
    )
    assert r2.status_code == 303
    assert "danger" in r2.headers["location"], "un tenant ajeno no puede activarse"
    assert "ac_tenant" not in r2.headers.get("set-cookie", "")


async def test_middleware_scopea_al_tenant_de_la_cookie(app_client, moto_setup) -> None:
    """Tras switch, el dashboard muestra la identidad del tenant de ejemplo."""
    _t1, t2 = moto_setup
    session = await _login(app_client)
    r = await app_client.post(
        "/admin/switch-tenant",
        headers={"Cookie": session},
        data={"tenant_id": str(t2)},
    )
    ac_tenant = next(
        c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c
    )
    r = await app_client.get("/admin/", headers={"Cookie": f"{session}; {ac_tenant}"})
    assert r.status_code == 200
    assert "Tenant de ejemplo (moto)" in r.text
    # el selector del sidebar está visible y lista el tenant activo
    assert 'name="tenant_id"' in r.text, "el selector de tenants debe estar presente"
    assert "Tenant de ejemplo (moto)" in r.text


async def test_selector_visible_en_rutas_del_pack(app_client, moto_setup) -> None:
    """C3: el selector de tenants NO desaparece en las rutas propias del pack (/crm)."""
    _t1, t2 = moto_setup
    session = await _login(app_client)
    r = await app_client.post(
        "/admin/switch-tenant",
        headers={"Cookie": session},
        data={"tenant_id": str(t2)},
    )
    ac_tenant = next(
        c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c
    )
    headers = {"Cookie": f"{session}; {ac_tenant}"}

    for path in ("/crm", "/crm/productos", "/crm/pedidos"):
        resp = await app_client.get(path, headers=headers)
        assert resp.status_code == 200, f"{path} debe renderizar"
        assert 'name="tenant_id"' in resp.text, f"selector de tenants visible en {path}"
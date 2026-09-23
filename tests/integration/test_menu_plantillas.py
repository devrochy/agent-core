"""Plantillas de WhatsApp: acceso desde la conexión (ADR-0031).

La entrada del menú lateral se quitó a pedido. El acceso es por el botón
"Plantillas" de cada fila de `MetaConnection` en `/conexiones`; la ruta
`/admin/plantillas` sigue existiendo (resuelve las conexiones del tenant y, con
varias, muestra un selector). Las plantillas son por conexión (WABA).
"""

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


async def _new_tenant() -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant menu plantillas {_uniq('x')}",
        slug=_uniq("menu-plantillas"),
        admin_email="admin@local",
    )
    return tid


async def _add_meta_connection(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            conn = MetaConnection(
                tenant_id=tid, channel="whatsapp", meta_app_id=_uniq("app"),
                status="connected", business_id=_uniq("biz"),
            )
            session.add(conn)
            await session.flush()
            return conn.id


async def test_sin_conexiones_redirige_a_conexiones(app_client):
    tid = await _new_tenant()
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    r = await app_client.get("/admin/plantillas", headers={"Cookie": cookie}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/conexiones")
    assert "flash" in r.headers["location"]


async def test_una_conexion_redirige_a_sus_plantillas(app_client):
    tid = await _new_tenant()
    conn_id = await _add_meta_connection(tid)
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    r = await app_client.get("/admin/plantillas", headers={"Cookie": cookie}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/conexiones/meta/{conn_id}/plantillas"


async def test_varias_conexiones_muestra_selector(app_client):
    tid = await _new_tenant()
    conn_a = await _add_meta_connection(tid)
    conn_b = await _add_meta_connection(tid)
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    r = await app_client.get("/admin/plantillas", headers={"Cookie": cookie})
    assert r.status_code == 200
    body = r.text
    assert "Plantillas de WhatsApp" in body
    assert str(conn_a) in body
    assert str(conn_b) in body


async def test_aislamiento_entre_tenants(app_client):
    """El selector de un tenant nunca muestra las conexiones de otro (RLS)."""
    tid_a = await _new_tenant()
    tid_b = await _new_tenant()
    conn_a = await _add_meta_connection(tid_a)
    conn_a2 = await _add_meta_connection(tid_a)  # fuerza el selector (2 conexiones)
    conn_b = await _add_meta_connection(tid_b)

    cookie = await _switch_tenant(app_client, await _login(app_client), tid_a)
    r = await app_client.get("/admin/plantillas", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert str(conn_a) in r.text and str(conn_a2) in r.text
    assert str(conn_b) not in r.text


async def test_plantillas_no_esta_en_el_menu_lateral(app_client):
    """La entrada del menú se quitó a pedido: el acceso es por la fila de la conexión."""
    tid = await _new_tenant()
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)
    r = await app_client.get("/admin", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert 'href="/admin/plantillas"' not in r.text


async def test_boton_plantillas_por_fila_en_conexiones(app_client):
    """El botón de plantillas aparece por fila de MetaConnection y apunta a ESA conexión."""
    tid = await _new_tenant()
    conn_id = await _add_meta_connection(tid)
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    r = await app_client.get("/admin/conexiones", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert f"/admin/conexiones/meta/{conn_id}/plantillas" in r.text


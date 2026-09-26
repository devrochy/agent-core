"""pack-calendar v2 en el panel: submenú `/calendar/*` (ADR-0061/0063/0069).

Con el pack habilitado para el tenant, el panel expone Conexión, Horarios y
calendario, Tipos de cita y Citas.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import unquote

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


async def _calendar_tenant(app_client) -> str:
    """Tenant nuevo con pack_calendar configurado y habilitado (flujo del panel)."""
    from core.onboarding import onboard_tenant

    slug = _uniq("calendar-v2")
    tid, _ = await onboard_tenant(
        name=f"Tenant calendar {slug}",
        slug=slug,
        business_description="Test del panel de calendario.",
        persona={"tone": "cercano"},
        admin_email="admin@local",
    )
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)
    r = await app_client.post(
        "/admin/plugins/pack_calendar/configurar",
        headers={"Cookie": cookie},
        data={
            "client_id": "cid-test",
            "client_secret": "secret-test",
            "calendar_id": "primary",
            "timezone": "America/Bogota",
            "event_duration_minutes": "45",
            "slot_minutes": "30",
            "buffer_minutes": "10",
            "business_hours": '{"mon": "09:00-18:00", "sun": ""}',
        },
    )
    assert r.status_code == 303
    return cookie


async def test_submenu_y_paginas_del_pack_calendar(app_client):
    cookie = await _calendar_tenant(app_client)

    # Conexión.
    r = await app_client.get("/calendar/", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Conectar cuenta de Google" in r.text
    # El submenú (secciones) se renderiza en el sidebar.
    assert "Tipos de cita" in r.text
    assert "Horarios y calendario" in r.text

    # Configuración: form + valores guardados.
    r = await app_client.get("/calendar/configuracion", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert 'name="business_hours"' in r.text
    assert "09:00-18:00" in r.text
    assert 'value="45"' in r.text

    # Tipos de cita: crear y listar.
    r = await app_client.post(
        "/calendar/tipos/guardar",
        headers={"Cookie": cookie},
        data={"name": f"Corte {_uniq('t')}", "duration_minutes": "30", "description": "prueba"},
    )
    assert r.status_code == 303
    r = await app_client.get("/calendar/tipos", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Corte" in r.text

    # Citas (vacío al inicio).
    r = await app_client.get("/calendar/citas", headers={"Cookie": cookie})
    assert r.status_code == 200


async def test_calendar_config_business_hours_invalido_rechaza(app_client):
    cookie = await _calendar_tenant(app_client)

    r = await app_client.post(
        "/calendar/configuracion",
        headers={"Cookie": cookie},
        data={
            "client_id": "cid",
            "calendar_id": "primary",
            "timezone": "America/Bogota",
            "event_duration_minutes": "60",
            "slot_minutes": "60",
            "buffer_minutes": "0",
            "business_hours": "no-es-json",
        },
    )
    assert r.status_code == 303
    assert "JSON" in unquote(r.headers["location"])
"""pack-calendar v2 en el panel: submenú `/calendar/*` (ADR-0061/0063/0069).

Con el pack habilitado para el tenant, el panel expone Conexión, Horarios y
calendario, Tipos de cita y Citas.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC
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


async def _enable_pack_calendar(tenant_id: uuid.UUID) -> None:
    from core.admin.views import _save_spec_version
    from core.agents.spec import load_active_spec
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        spec = await load_active_spec()
        new_spec = spec.model_copy(update={"packs": sorted({*spec.packs, "pack_calendar"})})
        await _save_spec_version(new_spec)


async def test_oauth_sin_config_no_da_500_y_guia_a_configurar(app_client):
    """Regresión del 500: `load_config` lanza si el pack no está configurado."""
    from core.onboarding import onboard_tenant

    slug = _uniq("calendar-nocfg")
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="x",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    # Sin configurar: /admin/plugins/pack_calendar/oauth redirige al configurador (no 500).
    r = await app_client.get("/admin/plugins/pack_calendar/oauth", headers={"Cookie": cookie})
    assert r.status_code == 303
    assert "/calendar/configuracion" in unquote(r.headers["location"])

    # Habilitado pero sin configurar: la página de conexión invita a configurar primero.
    await _enable_pack_calendar(tid)
    r = await app_client.get("/calendar/", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Configurar Google Calendar" in r.text

async def _calendar_tenant_tid(app_client):
    """Tenant con pack_calendar configurado+habilitado. Devuelve (cookie, tid)."""
    from core.onboarding import onboard_tenant

    slug = _uniq("calendar-json")
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="x",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)
    r = await app_client.post(
        "/admin/plugins/pack_calendar/configurar",
        headers={"Cookie": cookie},
        data={
            "client_id": "cid-test", "client_secret": "secret-test",
            "calendar_id": "primary", "timezone": "America/Bogota",
            "event_duration_minutes": "45", "slot_minutes": "30",
            "buffer_minutes": "10", "business_hours": '{"mon": "09:00-18:00"}',
        },
    )
    assert r.status_code == 303
    return cookie, tid


async def _make_appointment(tenant_id, *, title, start_utc, status="scheduled", google_event_id=None, attendee_email=None):
    from datetime import timedelta

    from pack_calendar.models import Appointment

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = Appointment(
                tenant_id=tenant_id, title=title, start_at=start_utc,
                end_at=start_utc + timedelta(minutes=60), status=status,
                google_event_id=google_event_id, attendee_email=attendee_email,
            )
            session.add(row)
            await session.flush()
            return row.id


async def _delete_appointment(tenant_id, appointment_id):
    from pack_calendar.models import Appointment

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = await session.get(Appointment, appointment_id)
            if row is not None:
                await session.delete(row)


async def test_citas_pagina_carga_fullcalendar(app_client):
    cookie, _ = await _calendar_tenant_tid(app_client)
    r = await app_client.get("/calendar/citas", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert 'id="calendar"' in r.text
    assert "fullcalendar" in r.text.lower()


async def test_calendario_json_cita_del_asistente(app_client, monkeypatch):
    from datetime import datetime

    cookie, tid = await _calendar_tenant_tid(app_client)
    start = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    appt_id = await _make_appointment(tid, title="Demo Plan Basic", start_utc=start, google_event_id="ev-1", attendee_email="a@b.com")
    cancelled_id = await _make_appointment(
        tid, title="Cancelada", start_utc=datetime(2026, 10, 6, 15, 0, tzinfo=UTC),
        status="cancelled", google_event_id="ev-2",
    )
    monkeypatch.setattr("pack_calendar.admin._fetch_google_events", lambda config, start, end: [])
    try:
        r = await app_client.get(
            "/calendar/citas/calendario.json?start=2026-10-01T00:00:00Z&end=2026-10-31T00:00:00Z",
            headers={"Cookie": cookie},
        )
        assert r.status_code == 200
        by_title = {e["title"]: e for e in r.json()["events"]}
        demo = by_title["Demo Plan Basic"]
        assert demo["extendedProps"]["source"] == "asistente"
        assert demo["extendedProps"]["status"] == "scheduled"
        assert demo["extendedProps"]["can_cancel"] is True
        assert demo["extendedProps"]["attendee_email"] == "a@b.com"
        assert demo["color"] == "#198754"
        canc = by_title["Cancelada"]
        assert canc["extendedProps"]["status"] == "cancelled"
        assert canc["extendedProps"]["can_cancel"] is False
        assert canc["color"] == "#6c757d"
    finally:
        await _delete_appointment(tid, appt_id)
        await _delete_appointment(tid, cancelled_id)


async def test_calendario_json_fusiona_eventos_de_google(app_client, monkeypatch):
    from datetime import datetime

    cookie, tid = await _calendar_tenant_tid(app_client)
    start = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
    appt_id = await _make_appointment(tid, title="Asistente con link", start_utc=start, google_event_id="ev-1")

    async def _fake(config, start, end):
        return [
            {
                "id": "ev-1", "status": "confirmed", "summary": "Asistente con link",
                "start": {"dateTime": "2026-10-06T10:00:00-05:00"},
                "end": {"dateTime": "2026-10-06T10:30:00-05:00"},
                "htmlLink": "https://calendar.google.com/calendar/r/ev-1",
            },
            {
                "id": "ev-99", "status": "confirmed", "summary": "Evento externo",
                "start": {"dateTime": "2026-10-07T11:00:00-05:00"},
                "end": {"dateTime": "2026-10-07T11:30:00-05:00"},
                "htmlLink": "https://calendar.google.com/calendar/r/ev-99",
            },
        ]

    monkeypatch.setattr("pack_calendar.admin._fetch_google_events", _fake)
    try:
        r = await app_client.get(
            "/calendar/citas/calendario.json?start=2026-10-01T00:00:00Z&end=2026-10-31T00:00:00Z",
            headers={"Cookie": cookie},
        )
        assert r.status_code == 200
        by_title = {e["title"]: e for e in r.json()["events"]}
        asist = by_title["Asistente con link"]
        assert asist["extendedProps"]["source"] == "asistente"
        assert asist["extendedProps"]["html_link"] == "https://calendar.google.com/calendar/r/ev-1"
        externo = by_title["Evento externo"]
        assert externo["extendedProps"]["source"] == "google"
        assert externo["color"] == "#0d6efd"
    finally:
        await _delete_appointment(tid, appt_id)

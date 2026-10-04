"""Calendario interno del pack-calendar (ADR-0084): agendar sin Google.

Crea/mueve/cancela citas en la agenda propia (BD) y `check_availability` calcula
los horarios libres reales. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Todos los días abiertos: el test no depende del día de la semana de la fecha elegida.
HORAS = '{"mon": "09:00-18:00", "tue": "09:00-18:00", "wed": "09:00-18:00", "thu": "09:00-18:00", "fri": "09:00-18:00", "sat": "09:00-18:00", "sun": "09:00-18:00"}'
FECHA = "2027-03-15"  # fecha fija futura (con todos los días abiertos es indiferente)


async def _tenant_calendario() -> uuid.UUID:
    from _api_helpers import ADMIN_EMAIL

    from core.onboarding import onboard_tenant

    slug = uniq("cal-int")
    tenant_id, _ = await onboard_tenant(
        name=f"Calendario interno {slug}",
        slug=slug,
        business_description="Agenda interna de prueba.",
        persona={"tone": "cercano"},
        packs=["pack_calendar"],
        admin_email=ADMIN_EMAIL,
    )
    return tenant_id


async def _configurar(tenant_id: uuid.UUID) -> None:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        await plugin_manager.save_config(
            "pack_calendar",
            {
                "source": "interno",
                "business_hours": HORAS,
                "timezone": "America/Bogota",
                "slot_minutes": 60,
                "buffer_minutes": 0,
                "event_duration_minutes": 60,
            },
        )


def _contexto() -> object:
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=str(uuid.uuid4()),
        channel="test",
    )


async def test_flujo_calendario_interno() -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()

    async with tenant_scope(tenant_id):
        # Disponibilidad sin citas: hay slots libres y no requiere Google.
        avail = await skill.check_availability({"date": FECHA}, ctx)
        assert avail["source"] == "interno"
        assert avail["business_hours"] == "09:00-18:00"
        assert "10:00" in avail["free_slots"]

        # Crear una cita a las 10:00 (sin Google).
        created = await skill.create_event(
            {"title": "Demo con Laura", "start_datetime": f"{FECHA}T10:00:00"}, ctx
        )
        assert created["status"] == "created"
        assert created["event_id"]

        # El slot ocupado ya no está libre.
        avail2 = await skill.check_availability({"date": FECHA}, ctx)
        assert "10:00" not in avail2["free_slots"]

        # Aparece listada.
        listed = await skill.list_appointments({}, ctx)
        assert listed["total"] == 1
        event_id = listed["appointments"][0]["event_id"]

        # Reagendar (mueve, no duplica).
        moved = await skill.update_event(
            {"start_datetime": f"{FECHA}T11:00:00", "event_id": event_id}, ctx
        )
        assert moved["status"] == "updated"
        listed2 = await skill.list_appointments({}, ctx)
        assert listed2["total"] == 1

        # Cancelar.
        cancelled = await skill.cancel_event({"event_id": event_id}, ctx)
        assert cancelled["status"] == "cancelled"
        listed3 = await skill.list_appointments({}, ctx)
        assert listed3["total"] == 0


async def test_bloqueo_de_agenda_ocupa_slot() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pack_calendar.models import CalendarBlock
    from pack_calendar.skills import CalendarSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    tz = ZoneInfo("America/Bogota")

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            session.add(
                CalendarBlock(
                    tenant_id=tenant_id,
                    start_at=datetime.fromisoformat(f"{FECHA}T12:00:00").replace(tzinfo=tz),
                    end_at=datetime.fromisoformat(f"{FECHA}T13:00:00").replace(tzinfo=tz),
                    reason="Almuerzo",
                )
            )

        skill = CalendarSkill()
        avail = await skill.check_availability({"date": FECHA}, _contexto())
        assert "12:00" not in avail["free_slots"]
        assert any("Almuerzo" in b.get("summary", "") for b in avail["busy_slots"])


async def test_dia_completamente_bloqueado_no_ofrece_slots() -> None:
    """HU-CAL-01: un bloqueo que cubre todo el horario deja el día sin horarios libres."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pack_calendar.models import CalendarBlock
    from pack_calendar.skills import CalendarSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    tz = ZoneInfo("America/Bogota")

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            session.add(
                CalendarBlock(
                    tenant_id=tenant_id,
                    start_at=datetime.fromisoformat(f"{FECHA}T00:00:00").replace(tzinfo=tz),
                    end_at=datetime.fromisoformat(f"{FECHA}T23:59:00").replace(tzinfo=tz),
                    reason="Feriado",
                )
            )

        avail = await CalendarSkill().check_availability({"date": FECHA}, _contexto())
        assert avail["free_slots"] == []


async def test_crud_bloqueos_api(app_client) -> None:
    """HU-CAL-01: crear, listar y eliminar bloqueos por la API admin, con aislamiento por tenant."""
    from _api_helpers import api_headers

    tid = await _tenant_calendario()
    headers = await api_headers(app_client, tid)

    # Vacío al inicio.
    r = await app_client.get("/api/v1/pack/calendar/blocks", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == []

    # Crear.
    payload = {
        "start_at": f"{FECHA}T12:00:00-05:00",
        "end_at": f"{FECHA}T13:00:00-05:00",
        "reason": "Almuerzo",
    }
    r = await app_client.post("/api/v1/pack/calendar/blocks", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    block = r.json()
    assert block["reason"] == "Almuerzo"
    block_id = block["id"]

    # Rango inválido.
    r = await app_client.post(
        "/api/v1/pack/calendar/blocks",
        headers=headers,
        json={"start_at": f"{FECHA}T13:00:00-05:00", "end_at": f"{FECHA}T12:00:00-05:00"},
    )
    assert r.status_code == 400

    # Listar.
    r = await app_client.get("/api/v1/pack/calendar/blocks", headers=headers)
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Otro tenant no lo ve (aislamiento / RLS).
    otro = await _tenant_calendario()
    otros_headers = await api_headers(app_client, otro)
    r = await app_client.get("/api/v1/pack/calendar/blocks", headers=otros_headers)
    assert r.status_code == 200
    assert r.json() == []

    # Eliminar.
    r = await app_client.delete(f"/api/v1/pack/calendar/blocks/{block_id}", headers=headers)
    assert r.status_code == 204
    r = await app_client.get("/api/v1/pack/calendar/blocks", headers=headers)
    assert r.json() == []

    # Vista semanal: 7 días y el horario de atención de cada uno.
    r = await app_client.get(
        "/api/v1/pack/calendar/week", headers=headers, params={"start": FECHA}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["days"]) == 7
    assert all("business_hours" in d for d in data["days"])

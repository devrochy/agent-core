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


async def _seed_email(tenant_id: uuid.UUID, conversation_id: uuid.UUID, *emails: str) -> None:
    """Siembra el email como mensaje INBOUND del cliente (procedencia del email).

    `create_calendar_event` solo acepta emails que el cliente haya escrito (o
    estén en su perfil): sin esto, la tool responde needs_data. Crea el
    `Customer`/`Conversation` mínimos (el mensaje tiene FK a conversations).
    """
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            customer = Customer(
                tenant_id=tenant_id,
                channel="whatsapp",
                external_id=f"wa-seed-{conversation_id.hex[:8]}",
                name="Test seed",
            )
            session.add(customer)
            await session.flush()
            session.add(
                Conversation(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    channel="whatsapp",
                    status="open",
                )
            )
            await session.flush()
            session.add(
                Message(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    direction="inbound",
                    sender_type="customer",
                    content="mis correos: " + ", ".join(emails),
                    status="sent",
                    client_message_id=str(uuid.uuid4()),
                )
            )


async def test_flujo_calendario_interno() -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "laura@ejemplo.com")

    async with tenant_scope(tenant_id):
        # Disponibilidad sin citas: hay slots libres y no requiere Google.
        avail = await skill.check_availability({"date": FECHA}, ctx)
        assert avail["source"] == "interno"
        assert avail["business_hours"] == "09:00-18:00"
        assert "10:00" in avail["free_slots"]

        # Crear una cita a las 10:00 (sin Google).
        created = await skill.create_event(
            {
                "title": "Demo con Laura",
                "start_datetime": f"{FECHA}T10:00:00",
                "attendee_email": "laura@ejemplo.com",
            },
            ctx,
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


async def test_create_event_rechaza_solapamiento_y_pasado() -> None:
    """HU-CAL-02: validación DURA en el servidor — no doble reserva y no fechas pasadas."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(
        tenant_id, ctx.conversation_id, "d1@ejemplo.com", "d2@ejemplo.com",
        "d3@ejemplo.com", "d4@ejemplo.com",
    )

    async with tenant_scope(tenant_id):
        first = await skill.create_event(
            {"title": "Demo 1", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "d1@ejemplo.com"},
            ctx,
        )
        assert first["status"] == "created"

        choque = await skill.create_event(
            {"title": "Demo 2", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "d2@ejemplo.com"},
            ctx,
        )
        assert choque["status"] == "rejected"
        assert choque["reason"] == "occupied"
        assert (await skill.list_appointments({}, ctx))["total"] == 1

        fuera = await skill.create_event(
            {"title": "Demo 3", "start_datetime": f"{FECHA}T22:00:00", "attendee_email": "d3@ejemplo.com"},
            ctx,
        )
        assert fuera["reason"] in ("outside_hours", "in_past")

        pasado = await skill.create_event(
            {"title": "Demo 4", "start_datetime": "2020-01-01T10:00:00", "attendee_email": "d4@ejemplo.com"},
            ctx,
        )
        assert pasado["reason"] == "in_past"


async def test_email_fallback_sin_conexion() -> None:
    """HU-CAL-02: sin conexión SMTP, agendar igual funciona; el correo queda no enviado."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "cliente@ejemplo.com")

    async with tenant_scope(tenant_id):
        res = await CalendarSkill().create_event(
            {
                "title": "Demo con correo",
                "start_datetime": f"{FECHA}T09:00:00",
                "attendee_email": "cliente@ejemplo.com",
            },
            ctx,
        )
        assert res["status"] == "created"
        assert res["email_sent"] is False
        assert res["email_reason"] in ("no_connection", "sin_destinatarios", "error_envio")


async def test_events_activities_y_patch_api(app_client) -> None:
    """HU-CAL-02/03: /events, historial y PATCH de cita con validación de choque."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from _api_helpers import api_headers

    tid = await _tenant_calendario()
    headers = await api_headers(app_client, tid)
    tz = ZoneInfo("America/Bogota")
    base = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=3)
    fecha = base.strftime("%Y-%m-%d")
    fin = (base + timedelta(days=1)).strftime("%Y-%m-%d")

    r = await app_client.post("/api/v1/pack/calendar/appointments", headers=headers, json={
        "title": "Demo API",
        "start_at": f"{fecha}T10:00:00-05:00",
        "end_at": f"{fecha}T11:00:00-05:00",
        "attendee_email": "cliente@ejemplo.com",
    })
    assert r.status_code == 201, r.text
    appt = r.json()
    assert appt["created_by"] == "panel"
    appt_id = appt["id"]

    r = await app_client.get(
        "/api/v1/pack/calendar/events", headers=headers, params={"start": fecha, "end": fin}
    )
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert any(e["kind"] == "appointment" and e["id"] == f"appt-{appt_id}" for e in events)

    r = await app_client.patch(
        f"/api/v1/pack/calendar/appointments/{appt_id}", headers=headers,
        json={"start_at": f"{fecha}T11:00:00-05:00", "end_at": f"{fecha}T12:00:00-05:00", "notify": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["start_at"].startswith(f"{fecha}T11:00")

    r2 = await app_client.post("/api/v1/pack/calendar/appointments", headers=headers, json={
        "title": "Otra", "start_at": f"{fecha}T15:00:00-05:00", "end_at": f"{fecha}T16:00:00-05:00",
    })
    assert r2.status_code == 201
    r = await app_client.patch(
        f"/api/v1/pack/calendar/appointments/{appt_id}", headers=headers,
        json={"start_at": f"{fecha}T15:00:00-05:00", "end_at": f"{fecha}T16:00:00-05:00"},
    )
    assert r.status_code == 400

    r = await app_client.get(
        f"/api/v1/pack/calendar/appointments/{appt_id}/activities", headers=headers
    )
    assert r.status_code == 200, r.text
    assert "reprogramada" in [a["action"] for a in r.json()]


async def test_block_patch_api(app_client) -> None:
    """HU-CAL-02: PATCH de bloqueo (arrastrar/redimensionar)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from _api_helpers import api_headers

    tid = await _tenant_calendario()
    headers = await api_headers(app_client, tid)
    tz = ZoneInfo("America/Bogota")
    fecha = (
        datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=5)
    ).strftime("%Y-%m-%d")

    r = await app_client.post("/api/v1/pack/calendar/blocks", headers=headers, json={
        "start_at": f"{fecha}T12:00:00-05:00", "end_at": f"{fecha}T13:00:00-05:00", "reason": "Almuerzo",
    })
    assert r.status_code == 201, r.text
    bid = r.json()["id"]

    r = await app_client.patch(f"/api/v1/pack/calendar/blocks/{bid}", headers=headers, json={
        "end_at": f"{fecha}T14:00:00-05:00", "reason": "Almuerzo largo",
    })
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "Almuerzo largo"


async def test_create_sin_email_pide_datos_y_no_crea() -> None:
    """Corrección post-prueba: sin email la tool NO crea y devuelve needs_data."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "ok@ejemplo.com")

    async with tenant_scope(tenant_id):
        res = await skill.create_event(
            {"title": "Demo sin email", "start_datetime": f"{FECHA}T10:00:00"}, ctx
        )
        assert res["status"] == "needs_data"
        assert "attendee_email" in res["missing"]
        assert res.get("error")
        # No se persistió nada.
        assert (await skill.list_appointments({}, ctx))["total"] == 0

        # Email inválido también pide el dato.
        malo = await skill.create_event(
            {"title": "Demo email malo", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "no-es-email"},
            ctx,
        )
        assert malo["status"] == "needs_data"

        # Email válido pero INVENTADO (no lo dio el cliente) → needs_data.
        inventado = await skill.create_event(
            {"title": "Demo email inventado", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "rob@example.com"},
            ctx,
        )
        assert inventado["status"] == "needs_data"

        # Con email válido y dado por el cliente sí crea.
        ok = await skill.create_event(
            {"title": "Demo con email", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "ok@ejemplo.com"},
            ctx,
        )
        assert ok["status"] == "created"


async def test_cancel_con_event_id_invalido_usa_fallback() -> None:
    """Corrección post-prueba: un event_id inventado no impide cancelar; el
    sistema busca la cita del cliente por la conversación."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "cancel@ejemplo.com")

    async with tenant_scope(tenant_id):
        created = await skill.create_event(
            {
                "title": "Demo a cancelar",
                "start_datetime": f"{FECHA}T11:00:00",
                "attendee_email": "cancel@ejemplo.com",
            },
            ctx,
        )
        assert created["status"] == "created"

        res = await skill.cancel_event(
            {"event_id": "deadbeef-0000-0000-0000-000000000001"}, ctx
        )
        assert res["status"] == "cancelled", res
        assert (await skill.list_appointments({}, ctx))["total"] == 0


async def test_update_con_event_id_invalido_usa_fallback() -> None:
    """Corrección post-prueba: un event_id inventado no impide reagendar."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "mover@ejemplo.com")

    async with tenant_scope(tenant_id):
        created = await skill.create_event(
            {
                "title": "Demo a mover",
                "start_datetime": f"{FECHA}T12:00:00",
                "attendee_email": "mover@ejemplo.com",
            },
            ctx,
        )
        assert created["status"] == "created"

        res = await skill.update_event(
            {
                "start_datetime": f"{FECHA}T13:00:00",
                "event_id": "deadbeef-0000-0000-0000-000000000002",
            },
            ctx,
        )
        assert res["status"] == "updated", res

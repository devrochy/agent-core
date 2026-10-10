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

    from core.alta_negocio import onboard_tenant

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
    # Día hábil: con "hoy + 3" un jueves caía en domingo y el PATCH a las 11:00 daba
    # `outside_hours` (falló en CI el 2026-10-08).
    while base.weekday() >= 5:
        base += timedelta(days=1)
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


async def test_cancel_por_numero_de_lista_cancela_la_correcta() -> None:
    """Corrección 2026-10-05: `event_id: "2"` (el número de la lista) cancela la
    SEGUNDA cita futura, no "la más próxima" (que canceló la equivocada)."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "n1@ejemplo.com", "n2@ejemplo.com")

    async with tenant_scope(tenant_id):
        primera = await skill.create_event(
            {"title": "Demo 1", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "n1@ejemplo.com"},
            ctx,
        )
        segunda = await skill.create_event(
            {"title": "Demo 2", "start_datetime": f"{FECHA}T12:00:00", "attendee_email": "n2@ejemplo.com"},
            ctx,
        )
        assert primera["status"] == "created" and segunda["status"] == "created"

        # El modelo pasa el número de la lista, no el id real.
        res = await skill.cancel_event({"event_id": "2"}, ctx)
        assert res["status"] == "cancelled", res
        assert segunda["event_id"] in res["cancelled"]

        # Queda la primera (no se canceló la equivocada).
        restantes = await skill.list_appointments({}, ctx)
        assert restantes["total"] == 1
        assert restantes["appointments"][0]["event_id"] == primera["event_id"]


async def test_cancel_con_id_inventado_y_varias_citas_no_cancela_nada() -> None:
    """Corrección 2026-10-05: con varias citas, un id inventado NO debe cancelar
    una arbitraria: devuelve not_found para que el asistente pida aclaración."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "v1@ejemplo.com", "v2@ejemplo.com")

    async with tenant_scope(tenant_id):
        await skill.create_event(
            {"title": "Demo A", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "v1@ejemplo.com"},
            ctx,
        )
        await skill.create_event(
            {"title": "Demo B", "start_datetime": f"{FECHA}T12:00:00", "attendee_email": "v2@ejemplo.com"},
            ctx,
        )
        res = await skill.cancel_event({"event_id": "demo-inventado-2026"}, ctx)
        assert res["status"] == "not_found", res
        # Ninguna se canceló.
        assert (await skill.list_appointments({}, ctx))["total"] == 2


# --------------------------------------------------------------------------
# HU-CAL-05: fechas futuras, próximos días libres, link de la cita y avisos
# desde el panel (D-SPEC-24-8/9/10).

# Domingo cerrado: `find_next_available` debe saltarlo.
HORAS_SIN_DOMINGO = '{"mon": "09:00-18:00", "tue": "09:00-18:00", "wed": "09:00-18:00", "thu": "09:00-18:00", "fri": "09:00-18:00", "sat": "09:00-18:00", "sun": ""}'


async def _configurar_con(tenant_id: uuid.UUID, **overrides) -> None:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    config = {
        "source": "interno",
        "business_hours": HORAS,
        "timezone": "America/Bogota",
        "slot_minutes": 60,
        "buffer_minutes": 0,
        "event_duration_minutes": 60,
    }
    config.update(overrides)
    async with tenant_scope(tenant_id):
        await plugin_manager.save_config("pack_calendar", config)


def _hoy_bogota():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Bogota"))


async def test_check_availability_fecha_pasada_devuelve_in_past() -> None:
    from datetime import timedelta

    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    hoy = _hoy_bogota()
    ayer = (hoy - timedelta(days=1)).strftime("%Y-%m-%d")

    async with tenant_scope(tenant_id):
        res = await CalendarSkill().check_availability({"date": ayer}, _contexto())
        assert res["status"] == "in_past"
        assert res["today"] == hoy.strftime("%Y-%m-%d")
        assert "free_slots" not in res

        futura = await CalendarSkill().check_availability({"date": FECHA}, _contexto())
        assert futura["status"] == "ok"
        assert futura["today"] == hoy.strftime("%Y-%m-%d")


async def test_check_availability_hoy_no_ofrece_horas_pasadas() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar_con(
        tenant_id,
        business_hours=HORAS.replace("09:00-18:00", "00:00-23:59"),
    )
    tz = ZoneInfo("America/Bogota")
    hoy = _hoy_bogota()

    async with tenant_scope(tenant_id):
        res = await CalendarSkill().check_availability({"date": hoy.strftime("%Y-%m-%d")}, _contexto())
    for slot in res["free_slots"]:
        inicio = datetime.fromisoformat(f"{hoy:%Y-%m-%d}T{slot}:00").replace(tzinfo=tz)
        assert inicio > hoy, f"ofreció {slot}, que ya pasó"


async def test_find_next_available_ofrece_los_proximos_dias_con_cupo() -> None:
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    from pack_calendar.models import CalendarBlock
    from pack_calendar.skills import CalendarSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar_con(tenant_id, business_hours=HORAS_SIN_DOMINGO)
    tz = ZoneInfo("America/Bogota")
    hoy = _hoy_bogota().date()
    manana = hoy + timedelta(days=1)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            session.add(
                CalendarBlock(
                    tenant_id=tenant_id,
                    start_at=datetime.combine(manana, datetime.min.time()).replace(tzinfo=tz),
                    end_at=datetime.combine(manana, datetime.max.time()).replace(tzinfo=tz),
                    reason="Feriado",
                )
            )
        res = await CalendarSkill().find_next_available({"days": 14}, _contexto())

    assert res["status"] == "ok"
    assert res["today"] == hoy.isoformat()
    fechas = [date.fromisoformat(d["date"]) for d in res["days"]]
    assert fechas == sorted(fechas)
    assert 1 <= len(fechas) <= 5
    assert res["total_days"] == len(fechas)
    assert all(f >= hoy for f in fechas)
    assert manana not in fechas, "el día bloqueado no se ofrece"
    assert all(f.weekday() != 6 for f in fechas), "el domingo está cerrado"
    assert all(d["free_slots"] and d["weekday"] for d in res["days"])


async def test_find_next_available_valida_el_rango_sin_romper() -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    async with tenant_scope(tenant_id):
        res = await CalendarSkill().find_next_available({"days": 40}, _contexto())
    assert "error" in res


async def test_create_y_update_internos_traen_link_de_calendario(monkeypatch) -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    enviados: list[tuple[str, str, str]] = []

    async def _fake_queue(to: str, subject: str, body: str, *args, **kwargs):
        enviados.append((to, subject, body))
        return None

    monkeypatch.setattr("core.gateway.outbound_email.queue_outbound_email", _fake_queue)

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "laura@ejemplo.com")

    async with tenant_scope(tenant_id):
        skill = CalendarSkill()
        created = await skill.create_event(
            {
                "title": "Demo con Laura",
                "start_datetime": f"{FECHA}T10:00:00",
                "attendee_email": "laura@ejemplo.com",
            },
            ctx,
        )
        assert created["status"] == "created"
        assert "calendar.google.com/calendar/render?action=TEMPLATE" in created["_safe_append"]
        assert "dates=20270315T100000%2F20270315T110000" in created["_safe_append"]
        assert "ctz=America%2FBogota" in created["_safe_append"]
        assert created["email_sent"] is True

        updated = await skill.update_event(
            {"event_id": created["event_id"], "start_datetime": f"{FECHA}T14:00:00"}, ctx
        )
        assert updated["status"] == "updated"
        assert "dates=20270315T140000%2F20270315T150000" in updated["_safe_append"]

    cuerpos_cliente = [body for to, _s, body in enviados if to == "laura@ejemplo.com"]
    assert len(cuerpos_cliente) == 2
    assert all("calendar.google.com/calendar/render?action=TEMPLATE" in b for b in cuerpos_cliente)


async def test_panel_mueve_y_cancela_con_aviso_por_correo_y_whatsapp(app_client, monkeypatch) -> None:
    from _api_helpers import api_headers
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    correos: list[tuple[str, str]] = []
    whatsapps: list[tuple[uuid.UUID, str]] = []

    async def _fake_queue(to: str, subject: str, body: str, *args, **kwargs):
        correos.append((to, subject))
        return None

    async def _fake_send(conversation_id, text, *args, **kwargs):
        whatsapps.append((conversation_id, text))
        return None

    monkeypatch.setattr("core.gateway.outbound_email.queue_outbound_email", _fake_queue)
    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _fake_send)

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "rob@ejemplo.com")
    async with tenant_scope(tenant_id):
        created = await CalendarSkill().create_event(
            {"title": "Demo Rob", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "rob@ejemplo.com"},
            ctx,
        )
    appt_id = created["event_id"]
    correos.clear()
    headers = await api_headers(app_client, tenant_id)

    # Mover desde el panel con aviso: correo + WhatsApp, y la respuesta lo dice.
    r = await app_client.patch(
        f"/api/v1/pack/calendar/appointments/{appt_id}", headers=headers,
        json={"start_at": f"{FECHA}T15:00:00-05:00", "end_at": f"{FECHA}T16:00:00-05:00", "notify": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notification"] == {"email_sent": True, "email_reason": "", "whatsapp_sent": True}
    assert any(to == "rob@ejemplo.com" for to, _subject in correos)
    assert whatsapps[-1][0] == ctx.conversation_id
    assert "15:00" in whatsapps[-1][1]

    # Mover sin aviso: no manda nada.
    enviados_antes = (len(correos), len(whatsapps))
    r = await app_client.patch(
        f"/api/v1/pack/calendar/appointments/{appt_id}", headers=headers,
        json={"start_at": f"{FECHA}T16:00:00-05:00", "end_at": f"{FECHA}T17:00:00-05:00", "notify": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notification"] is None
    assert (len(correos), len(whatsapps)) == enviados_antes

    # Cancelar desde el panel (sin body = notify true): correo + WhatsApp + actividad.
    r = await app_client.post(f"/api/v1/pack/calendar/appointments/{appt_id}/cancel", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert r.json()["notification"]["email_sent"] is True
    assert r.json()["notification"]["whatsapp_sent"] is True
    r = await app_client.get(f"/api/v1/pack/calendar/appointments/{appt_id}/activities", headers=headers)
    assert "cancelada" in [a["action"] for a in r.json()]

    # Cancelar de nuevo es idempotente: no vuelve a avisar.
    enviados_antes = (len(correos), len(whatsapps))
    r = await app_client.post(f"/api/v1/pack/calendar/appointments/{appt_id}/cancel", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["notification"] is None
    assert (len(correos), len(whatsapps)) == enviados_antes


async def test_disponibilidad_no_ve_citas_de_otro_negocio() -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_a = await _tenant_calendario()
    tenant_b = await _tenant_calendario()
    await _configurar(tenant_a)
    await _configurar(tenant_b)
    ctx = _contexto()
    await _seed_email(tenant_a, ctx.conversation_id, "a@ejemplo.com")

    async with tenant_scope(tenant_a):
        created = await CalendarSkill().create_event(
            {"title": "Cita A", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "a@ejemplo.com"},
            ctx,
        )
        assert created["status"] == "created"
        assert "10:00" not in (await CalendarSkill().check_availability({"date": FECHA}, ctx))["free_slots"]

    async with tenant_scope(tenant_b):
        assert "10:00" in (await CalendarSkill().check_availability({"date": FECHA}, _contexto()))["free_slots"]


async def test_listado_devuelve_hora_local_y_fecha_escrita() -> None:
    """E2E HU-CAL-06: `list_client_appointments` devolvía UTC y el asistente dijo
    "7 de octubre" para una cita del jueves 8. Ahora la hora va en la zona del
    negocio con la fecha escrita."""
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "rob@ejemplo.com")
    async with tenant_scope(tenant_id):
        skill = CalendarSkill()
        created = await skill.create_event(
            {"title": "Demo Rob", "start_datetime": f"{FECHA}T15:00:00", "attendee_email": "rob@ejemplo.com"},
            ctx,
        )
        assert created["fecha"] == "lunes 15 de marzo de 2027"
        assert created["hora"] == "15:00"
        listed = await skill.list_appointments({}, ctx)

    cita = listed["appointments"][0]
    assert cita["start"] == f"{FECHA}T15:00:00-05:00"
    assert cita["fecha"] == "lunes 15 de marzo de 2027"
    assert cita["hora"] == "15:00"


async def test_listado_incluye_las_pasadas_de_las_ultimas_24h() -> None:
    """Test 2026-10-10: a las 23:49 la clienta preguntó por su cita de las 10:00 de
    ese día (seguía en `scheduled`), la tool devolvió `total: 0` y el asistente
    negó que existiera. Las de las últimas 24 h salen con `ya_paso` y sin índice;
    "cancelá la 1" sigue resolviendo la primera FUTURA."""
    from datetime import UTC, datetime, timedelta

    from pack_calendar.models import Appointment
    from pack_calendar.skills import CalendarSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    skill = CalendarSkill()
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, "hoy@ejemplo.com")
    ahora = datetime.now(UTC)
    async with tenant_scope(tenant_id):
        async with get_session() as s:
            def _cita(title: str, horas: float) -> Appointment:
                inicio = ahora + timedelta(hours=horas)
                return Appointment(
                    tenant_id=tenant_id, conversation_id=ctx.conversation_id, title=title,
                    start_at=inicio, end_at=inicio + timedelta(hours=1), timezone="America/Bogota",
                )

            vieja, hoy = _cita("Demo de antier", -30), _cita("Demo de hoy", -14)
            s.add_all([vieja, hoy])
            await s.flush()
            hoy_id = str(hoy.id)
        futura = await skill.create_event(
            {"title": "Demo futura", "start_datetime": f"{FECHA}T10:00:00", "attendee_email": "hoy@ejemplo.com"},
            ctx,
        )
        assert futura["status"] == "created", futura
        listed = await skill.list_appointments({}, ctx)

        assert listed["total"] == 2 and listed["proximas"] == 1
        pasada, proxima = listed["appointments"]
        assert pasada["event_id"] == hoy_id and pasada["ya_paso"] is True and "index" not in pasada
        assert proxima["event_id"] == futura["event_id"] and proxima["ya_paso"] is False
        assert proxima["index"] == 1

        res = await skill.cancel_event({"event_id": "1"}, ctx)
        assert res["status"] == "cancelled", res
        assert res["cancelled"] == [futura["event_id"]]
        restantes = await skill.list_appointments({}, ctx)
        assert [c["event_id"] for c in restantes["appointments"]] == [hoy_id]

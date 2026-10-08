"""HU-SH-02 · demo agendada de punta a punta, contra la BD real.

Gherkin: sin doble reserva (la base lo garantiza aunque lleguen a la vez), confirmación con
fecha, hora, zona y enlace, recordatorios a 24 h y 1 h (una vez cada uno), aviso al equipo
(al responsable del tipo o a los dueños), asistencia con oferta de reagendar y cita desde
el panel en modo interno que avisa al cliente. «Sin horas pasadas» ya lo cubre
`test_calendar_interno.py::test_check_availability_hoy_no_ofrece_horas_pasadas`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_calendar_interno import FECHA, _configurar, _contexto, _seed_email, _tenant_calendario

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _eventos(tid, topic: str) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(select(DomainEvent).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic))
                ).scalars().all()
            )


async def _tipo(tid, **campos) -> uuid.UUID:
    from pack_calendar.models import AppointmentType

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            tipo = AppointmentType(tenant_id=tid, name="Demo", duration_minutes=60, **campos)
            s.add(tipo)
            await s.flush()
            return tipo.id


async def _admin_del_negocio(tid) -> uuid.UUID:
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as s:
        return (await s.execute(select(TenantUser.admin_id).where(TenantUser.tenant_id == tid))).scalars().first()


async def test_dos_reservas_a_la_vez_una_gana_y_la_otra_recibe_alternativas() -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    await _configurar(tid)
    ctx_a, ctx_b = _contexto(), _contexto()
    await _seed_email(tid, ctx_a.conversation_id, "a@ejemplo.com")
    await _seed_email(tid, ctx_b.conversation_id, "b@ejemplo.com")
    skill = CalendarSkill()

    async def _reservar(ctx, email):
        async with tenant_scope(tid):
            return await skill.create_event(
                {"title": "Demo", "start_datetime": f"{FECHA}T11:00:00", "attendee_email": email}, ctx
            )

    resultados = await asyncio.gather(_reservar(ctx_a, "a@ejemplo.com"), _reservar(ctx_b, "b@ejemplo.com"))
    estados = sorted(r["status"] for r in resultados)
    assert estados == ["created", "rejected"], resultados
    perdedor = next(r for r in resultados if r["status"] == "rejected")
    assert perdedor["reason"] == "occupied"


async def test_la_base_rechaza_el_solape_aunque_se_saltee_la_validacion() -> None:
    from pack_calendar.demo import HorarioOcupado
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    await _configurar(tid)
    inicio = datetime(2027, 3, 16, 15, tzinfo=UTC)
    skill = CalendarSkill()
    async with tenant_scope(tid):
        assert await skill._mirror_appointment({}, _contexto(), inicio, inicio + timedelta(hours=1), title="A")
        with pytest.raises(HorarioOcupado):
            await skill._mirror_appointment(
                {}, _contexto(), inicio + timedelta(minutes=30), inicio + timedelta(minutes=90), title="B"
            )


async def test_confirmacion_trae_fecha_hora_zona_y_enlace_del_tipo() -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    await _configurar(tid)
    tipo = await _tipo(tid, enlace_reunion="https://meet.example.com/demo")
    ctx = _contexto()
    await _seed_email(tid, ctx.conversation_id, "c@ejemplo.com")
    async with tenant_scope(tid):
        r = await CalendarSkill().create_event(
            {"start_datetime": f"{FECHA}T09:00:00", "attendee_email": "c@ejemplo.com", "appointment_type_id": str(tipo)},
            ctx,
        )
    assert r["status"] == "created", r
    confirmacion = r["_safe_append"]
    assert "09:00" in confirmacion and "America/Bogota" in confirmacion
    assert "https://meet.example.com/demo" in confirmacion


async def test_cita_nueva_avisa_al_responsable_del_tipo() -> None:
    from pack_calendar.models import Appointment
    from pack_calendar.skills import CalendarSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    await _configurar(tid)
    responsable = await _admin_del_negocio(tid)
    tipo = await _tipo(tid, responsable_admin_id=responsable)
    ctx = _contexto()
    await _seed_email(tid, ctx.conversation_id, "r@ejemplo.com")
    async with tenant_scope(tid):
        r = await CalendarSkill().create_event(
            {"start_datetime": f"{FECHA}T13:00:00", "attendee_email": "r@ejemplo.com", "appointment_type_id": str(tipo)},
            ctx,
        )
        async with get_session() as s:
            cita = await s.get(Appointment, uuid.UUID(r["event_id"]))
    assert cita.asignado_admin_id == responsable
    cambios = await _eventos(tid, "appointment.changed")
    assert [e.payload["accion"] for e in cambios] == ["programada"]


async def test_avisar_al_equipo_va_al_asignado_o_a_los_duenos(monkeypatch) -> None:
    from pack_calendar.demo import avisar_al_equipo
    from pack_calendar.models import Appointment

    import core.tasks.push_tasks as push
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    enviados: list[tuple] = []
    monkeypatch.setattr(push.send_push_batch, "delay", lambda *a, **k: enviados.append(("asignado", a[1])))
    monkeypatch.setattr(push.notify_owners_push, "delay", lambda *a, **k: enviados.append(("duenos", None)))
    tid = await _tenant_calendario()
    asignado = await _admin_del_negocio(tid)  # colaborador real del negocio
    inicio = datetime(2027, 3, 17, 15, tzinfo=UTC)
    async with tenant_scope(tid):
        async with get_session() as s:
            con = Appointment(tenant_id=tid, title="Demo", start_at=inicio, end_at=inicio + timedelta(hours=1), asignado_admin_id=asignado)
            sin = Appointment(tenant_id=tid, title="Demo", start_at=inicio + timedelta(hours=2), end_at=inicio + timedelta(hours=3))
            s.add_all([con, sin])
            await s.flush()
            ids = (str(con.id), str(sin.id))
        await avisar_al_equipo(ids[0], "programada", "Demo", inicio.isoformat())
        await avisar_al_equipo(ids[1], "cancelada", "Demo", None)
    assert enviados == [("asignado", [str(asignado)]), ("duenos", None)]


async def test_recordatorios_a_24h_y_1h_una_vez_cada_uno() -> None:
    from pack_calendar.jobs import _recordatorios_de_citas
    from pack_calendar.models import Appointment

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    await _configurar(tid)
    ahora = datetime.now(UTC)
    cliente = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as s:
            manana = Appointment(
                tenant_id=tid, customer_id=cliente, title="Demo", start_at=ahora + timedelta(hours=23, minutes=50),
                end_at=ahora + timedelta(hours=24, minutes=50), created_at=ahora - timedelta(days=2),
            )
            en_una_hora = Appointment(
                tenant_id=tid, customer_id=cliente, title="Demo", start_at=ahora + timedelta(minutes=55),
                end_at=ahora + timedelta(hours=2), created_at=ahora - timedelta(days=2),
            )
            recien = Appointment(  # agendada hace 1 h para dentro de 20 h: no recibe el de 24 h
                tenant_id=tid, customer_id=cliente, title="Demo", start_at=ahora + timedelta(hours=20),
                end_at=ahora + timedelta(hours=21), created_at=ahora - timedelta(hours=1),
            )
            s.add_all([manana, en_una_hora, recien])
            await s.flush()
            ids = {str(manana.id): 24, str(en_una_hora.id): 1}
    await _recordatorios_de_citas(ahora)
    await _recordatorios_de_citas(ahora + timedelta(minutes=5))
    eventos = await _eventos(tid, "appointment.reminder")
    assert {(e.payload["appointment_id"], e.payload["horas"]) for e in eventos} == set(ids.items())
    assert len(eventos) == 2
    assert all(e.payload["zona"] == "America/Bogota" for e in eventos)


async def test_no_asistio_ofrece_reagendar_y_no_se_marca_una_futura() -> None:
    from pack_calendar.admin_api import AsistenciaIn, marcar_asistencia
    from pack_calendar.models import Appointment

    from core.api.deps import Principal
    from core.api.errors import ApiError
    from core.db.models import Admin
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    ahora = datetime.now(UTC)
    async with tenant_scope(tid):
        async with get_session() as s:
            pasada = Appointment(tenant_id=tid, customer_id=uuid.uuid4(), title="Demo", start_at=ahora - timedelta(hours=3), end_at=ahora - timedelta(hours=2))
            futura = Appointment(tenant_id=tid, customer_id=uuid.uuid4(), title="Demo", start_at=ahora + timedelta(days=1), end_at=ahora + timedelta(days=1, hours=1))
            s.add_all([pasada, futura])
            await s.flush()
            ids = (pasada.id, futura.id)
        admin = Admin(id=uuid.uuid4(), email="a@x.test", username=None, password_hash="x", role="master", active=True)
        principal = Principal(admin=admin, tenant_id=tid, tenants=[], permissions={"pack_calendar:citas": "write"})
        out = await marcar_asistencia(ids[0], AsistenciaIn(asistio=False), principal)
        await marcar_asistencia(ids[0], AsistenciaIn(asistio=False), principal)  # idempotente
        with pytest.raises(ApiError):
            await marcar_asistencia(ids[1], AsistenciaIn(asistio=True), principal)
    assert out.status == "no_asistio"
    ofertas = await _eventos(tid, "appointment.no_show")
    assert [e.payload["appointment_id"] for e in ofertas] == [str(ids[0])]


async def test_cita_desde_el_panel_en_modo_interno_avisa_al_cliente() -> None:
    from pack_calendar.admin_api import AppointmentIn, create_appointment

    from core.api.deps import Principal
    from core.api.errors import ApiError
    from core.db.models import Admin, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    await _configurar(tid)
    async with tenant_scope(tid):
        async with get_session() as s:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"wa-{uuid.uuid4().hex[:6]}", name="Ana")
            s.add(cliente)
            await s.flush()
            cliente_id = cliente.id
        admin = Admin(id=uuid.uuid4(), email="a@x.test", username=None, password_hash="x", role="master", active=True)
        principal = Principal(admin=admin, tenant_id=tid, tenants=[], permissions={"pack_calendar:citas": "write"})
        inicio = datetime(2027, 3, 18, 15, tzinfo=UTC)
        out = await create_appointment(
            AppointmentIn(title="Demo", start_at=inicio, end_at=inicio + timedelta(hours=1), customer_id=cliente_id), principal
        )
        with pytest.raises(ApiError) as exc:  # mismo horario: la base lo rechaza
            await create_appointment(
                AppointmentIn(title="Demo 2", start_at=inicio, end_at=inicio + timedelta(hours=1)), principal
            )
    assert out.google_event_id is None
    assert exc.value.code == "horario_ocupado"
    avisos = await _eventos(tid, "appointment.confirmed")
    assert [e.payload["customer_id"] for e in avisos] == [str(cliente_id)]
    assert [e.payload["accion"] for e in await _eventos(tid, "appointment.changed")] == ["programada"]


# ------------------------------------------------------------- auditoría HU-SH-02
async def test_el_asistente_no_ve_ni_toca_citas_de_otro_cliente() -> None:
    from pack_calendar.skills import CalendarSkill

    from core.shared.tenant import tenant_scope

    tid = await _tenant_calendario()
    await _configurar(tid)
    ctx_ana, ctx_otro = _contexto(), _contexto()
    await _seed_email(tid, ctx_ana.conversation_id, "ana@ejemplo.com")
    skill = CalendarSkill()
    async with tenant_scope(tid):
        creada = await skill.create_event(
            {"title": "Demo con Ana Pérez", "start_datetime": f"{FECHA}T14:00:00", "attendee_email": "ana@ejemplo.com"},
            ctx_ana,
        )
        assert creada["status"] == "created"
        ajenas = await skill.list_appointments({"title": "Ana"}, ctx_otro)
        cancelar = await skill.cancel_event({"title": "Demo con Ana", "attendee_email": "ana@ejemplo.com"}, ctx_otro)
        propias = await skill.list_appointments({}, ctx_ana)
        ocupados = await skill.check_availability({"date": FECHA}, ctx_otro)
    assert ajenas["total"] == 0
    assert cancelar.get("status") != "cancelled"
    assert propias["total"] == 1
    assert all("Ana" not in str(b.get("summary")) for b in ocupados.get("busy_slots", []))


async def test_un_ex_colaborador_no_recibe_el_aviso(monkeypatch) -> None:
    from pack_calendar.demo import avisar_al_equipo
    from pack_calendar.models import Appointment

    import core.tasks.push_tasks as push
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    enviados: list[str] = []
    monkeypatch.setattr(push.send_push_batch, "delay", lambda *a, **k: enviados.append("asignado"))
    monkeypatch.setattr(push.notify_owners_push, "delay", lambda *a, **k: enviados.append("duenos"))
    tid = await _tenant_calendario()
    inicio = datetime(2027, 3, 19, 15, tzinfo=UTC)
    async with tenant_scope(tid):
        async with get_session() as s:
            cita = Appointment(tenant_id=tid, title="Demo", start_at=inicio, end_at=inicio + timedelta(hours=1), asignado_admin_id=uuid.uuid4())
            s.add(cita)
            await s.flush()
            cita_id = str(cita.id)
        await avisar_al_equipo(cita_id, "programada", "Demo", inicio.isoformat())
    assert enviados == ["duenos"]

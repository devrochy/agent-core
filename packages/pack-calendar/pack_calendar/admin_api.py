"""API admin del pack de Calendario (ADR-0079).

Montada en `/api/v1/pack/calendar`. Estado de la conexión, tipos de cita y citas
(listar y cancelar). La configuración del pack ya se edita con el formulario
genérico de JSON Schema en `/habilidades/pack_calendar`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.session import get_owned, get_session

# Registra los providers de búsqueda global del pack (ADR-0096).
from . import search as _search  # noqa: F401
from . import sync as _sync  # noqa: F401
from .models import Appointment, AppointmentType

PLUGIN_ID = "pack_calendar"
router = APIRouter(prefix="/api/v1/pack/calendar", tags=["pack · calendario"])


# ------------------------------------------------------------------ estado
class CalendarStatus(BaseModel):
    configured: bool
    connected: bool
    account_email: str
    connection_label: str
    timezone: str
    default_duration_minutes: int


@router.get("/status", response_model=CalendarStatus)
async def calendar_status(
    principal: Annotated[Principal, Depends(require("pack_calendar:conexion"))],
) -> CalendarStatus:
    from core.gateway.connections import get_active_connection
    from core.plugin_manager import plugin_manager
    from core.shared.exceptions import PluginConfigError

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except PluginConfigError:
        config = None
    connection = await get_active_connection("google_calendar")
    return CalendarStatus(
        configured=config is not None,
        connected=connection is not None,
        account_email=(connection.external_id if connection else "") or "",
        connection_label=(connection.label if connection else "") or "",
        timezone=getattr(config, "timezone", "America/Bogota") or "America/Bogota",
        default_duration_minutes=int(getattr(config, "event_duration_minutes", 60) or 60),
    )


# ------------------------------------------------------------------ tipos de cita
class TypeOut(BaseModel):
    id: uuid.UUID
    name: str
    duration_minutes: int
    description: str
    active: bool


class TypeSave(BaseModel):
    name: str
    duration_minutes: int = 60
    description: str = ""


@router.get("/types", response_model=list[TypeOut])
async def list_types(
    principal: Annotated[Principal, Depends(require("pack_calendar:tipos"))],
) -> list[TypeOut]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(AppointmentType)
                    .where(
                        AppointmentType.tenant_id == principal.tenant_id,
                        AppointmentType.deleted_at.is_(None),
                    )
                    .order_by(AppointmentType.name)
                )
            )
            .scalars()
            .all()
        )
    return [
        TypeOut(
            id=t.id,
            name=t.name,
            duration_minutes=t.duration_minutes,
            description=t.description or "",
            active=bool(t.active),
        )
        for t in rows
    ]


@router.post("/types", status_code=204)
async def create_type(
    payload: TypeSave,
    principal: Annotated[Principal, Depends(require("pack_calendar:tipos", write=True))],
) -> Response:
    name = payload.name.strip()
    if not name:
        raise ApiError.bad_request("El nombre es obligatorio.", "missing_name")
    if principal.tenant_id is None:
        raise ApiError.bad_request("Sin negocio activo.", "no_tenant")
    async with get_session() as session:
        session.add(
            AppointmentType(
                tenant_id=principal.tenant_id,
                name=name,
                duration_minutes=max(5, payload.duration_minutes),
                description=payload.description,
            )
        )
    return Response(status_code=204)


@router.post("/types/{type_id}/delete", status_code=204)
async def delete_type(
    type_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:tipos", write=True))],
) -> Response:
    from core.db.models import utcnow

    async with get_session() as session:
        row = await get_owned(session, AppointmentType, type_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Tipo de cita no encontrado.")
        row.deleted_at = utcnow()
        row.active = False
    return Response(status_code=204)


# ------------------------------------------------------------------ citas
class NotificationOut(BaseModel):
    """Resultado real del aviso al cliente (HU-CAL-05)."""

    email_sent: bool
    email_reason: str
    whatsapp_sent: bool


async def _notify_client(
    action: str,
    *,
    title: str,
    start: datetime,
    end: datetime,
    attendee_email: str | None,
    conversation_id: uuid.UUID | None,
) -> NotificationOut:
    """Avisa al cliente por correo y WhatsApp cuando el negocio mueve (`moved`) o
    cancela (`cancelled`) una cita desde el panel (D-SPEC-24-10, paridad con el
    panel legado). Best-effort: un fallo de envío se informa, nunca se lanza."""
    from zoneinfo import ZoneInfo

    from core.plugin_manager import plugin_manager
    from core.shared.logger import get_logger
    from pack_calendar.config import (
        DEFAULT_CANCELLED_MESSAGE,
        DEFAULT_MOVED_MESSAGE,
        CalendarConfig,
    )
    from pack_calendar.skills import CalendarSkill, _calendar_link

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    if not isinstance(config, CalendarConfig):
        return NotificationOut(email_sent=False, email_reason="sin_configuracion", whatsapp_sent=False)

    skill = CalendarSkill()
    title = title or "tu cita"
    link = ""
    if action != "cancelled":
        business = await skill._business_name()
        link = _calendar_link(start, end, title, f"Cita agendada con {business}", config.timezone)
    local = start.astimezone(ZoneInfo(config.timezone))
    if action == "cancelled":
        template = config.appointment_cancelled_message or DEFAULT_CANCELLED_MESSAGE
    else:
        template = config.appointment_moved_message or DEFAULT_MOVED_MESSAGE
    try:
        whatsapp_sent = await skill._send_client_notice(
            conversation_id,
            template,
            fecha=local.strftime("%d/%m/%Y"),
            hora=local.strftime("%H:%M"),
            titulo=title,
            link=link,
        )
    except Exception:
        get_logger(__name__).warning("calendar_panel_whatsapp_failed", exc_info=True)
        whatsapp_sent = False
    email_sent, email_reason = await skill._notify_appointment_email(
        action, title=title, start=start, attendee_email=attendee_email, config=config, link=link
    )
    return NotificationOut(email_sent=email_sent, email_reason=email_reason, whatsapp_sent=whatsapp_sent)


class AppointmentOut(BaseModel):
    id: uuid.UUID
    title: str
    attendee_email: str | None
    start_at: datetime
    end_at: datetime
    status: str
    notes: str
    google_event_id: str | None
    appointment_type_id: uuid.UUID | None
    created_by: str
    timezone: str
    # Resultado del aviso al cliente al mover desde el panel; None si no se avisó.
    notification: NotificationOut | None = None


@router.get("/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    include_past: Annotated[bool, Query()] = False,
) -> list[AppointmentOut]:
    """Próximas citas (por defecto 30 días) del negocio."""
    now = datetime.now(UTC)
    start = now - timedelta(days=1) if include_past else now - timedelta(hours=1)
    end = now + timedelta(days=days)
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Appointment)
                    .where(
                        Appointment.tenant_id == principal.tenant_id,
                        Appointment.deleted_at.is_(None),
                        Appointment.start_at >= start,
                        Appointment.start_at <= end,
                    )
                    .order_by(Appointment.start_at)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    return [
        AppointmentOut(
            id=a.id,
            title=a.title,
            attendee_email=a.attendee_email,
            start_at=a.start_at,
            end_at=a.end_at,
            status=a.status,
            notes=a.notes or "",
            google_event_id=a.google_event_id,
            appointment_type_id=a.appointment_type_id,
            created_by=a.created_by,
            timezone=a.timezone,
        )
        for a in rows
    ]


class AppointmentCancel(BaseModel):
    notify: bool = True


class AppointmentCancelOut(BaseModel):
    status: str
    # Resultado del aviso al cliente; None si no se avisó (o ya estaba cancelada).
    notification: NotificationOut | None = None


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentCancelOut)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
    payload: Annotated[AppointmentCancel | None, Body()] = None,
) -> AppointmentCancelOut:
    """Cancela la cita: borra el evento en Google (best-effort), marca el espejo,
    registra la actividad y, con `notify` (default), avisa al cliente por correo
    y WhatsApp (HU-CAL-05). Idempotente: cancelar una cita ya cancelada no
    vuelve a avisar."""
    from core.db.models import utcnow
    from pack_calendar.skills import CalendarSkill

    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
    if row is None:
        raise ApiError.not_found("Cita no encontrada.")
    if row.status == "cancelled":
        return AppointmentCancelOut(status="cancelled")

    if row.google_event_id:
        try:
            import httpx

            from core.plugin_manager import plugin_manager
            from pack_calendar.oauth import get_access_token

            config = await plugin_manager.load_config(PLUGIN_ID)
            token = await get_access_token(
                config.client_id, config.client_secret.get_secret_value()
            )
            async with httpx.AsyncClient(timeout=30) as client:
                await client.delete(
                    "https://www.googleapis.com/calendar/v3/calendars/"
                    f"{config.calendar_id}/events/{row.google_event_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception:
            from core.shared.logger import get_logger

            get_logger(__name__).warning(
                "calendar_google_cancel_failed", appointment_id=str(appointment_id)
            )

    async with get_session() as session:
        row_db = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
        if row_db is not None:
            row_db.status = "cancelled"
            row_db.updated_at = utcnow()

    await CalendarSkill()._record_event(
        action="cancelada",
        title=row.title or "",
        appointment_id=row.id,
        conversation_id=row.conversation_id,
        old_start=row.start_at,
        actor="panel",
    )
    notification = None
    if payload is None or payload.notify:
        notification = await _notify_client(
            "cancelled",
            title=row.title,
            start=row.start_at,
            end=row.end_at,
            attendee_email=row.attendee_email,
            conversation_id=row.conversation_id,
        )
    return AppointmentCancelOut(status="cancelled", notification=notification)


class AppointmentIn(BaseModel):
    title: str
    start_at: datetime
    end_at: datetime
    attendee_email: str = ""
    notes: str = ""
    appointment_type_id: uuid.UUID | None = None


@router.post("/appointments", response_model=AppointmentOut, status_code=201)
async def create_appointment(
    payload: AppointmentIn,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
) -> AppointmentOut:
    """Crea una cita: local y, si el negocio tiene Google conectado, también el evento real."""
    from core.plugin_manager import plugin_manager
    from pack_calendar.admin import PLUGIN_ID, _google_create_event, _google_token
    from pack_calendar.models import Appointment

    titulo = payload.title.strip()
    if not titulo:
        raise ApiError.bad_request("El título es obligatorio.", "missing_title")
    if payload.end_at <= payload.start_at:
        raise ApiError.bad_request("La hora de fin debe ser posterior al inicio.", "invalid_range")

    attendee = payload.attendee_email.strip() or None
    notas = payload.notes.strip()
    google_event_id: str | None = None
    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        # Sin configuración del pack (p. ej. admin master en un negocio que no lo
        # tiene habilitado) la cita es local: no hay Google al que empujarla.
        config = None
    # Solo se empuja a Google en modo `google`; en `interno` la cita es local
    # (antes se intentaba Google aunque el modo fuera interno → error 400).
    if config is not None and getattr(config, "source", "interno") == "google":
        try:
            token = await _google_token(config)
            evento = await _google_create_event(
                config,
                token,
                title=titulo,
                start=payload.start_at,
                end=payload.end_at,
                attendee_email=attendee,
                notes=notas,
            )
            google_event_id = evento.get("id")
        except Exception as exc:
            raise ApiError.bad_request(
                "Google Calendar rechazó la creación (revisá la conexión).", "google_rejected"
            ) from exc

    async with get_session() as session:
        cita = Appointment(
            tenant_id=principal.tenant_id,
            appointment_type_id=payload.appointment_type_id,
            title=titulo,
            attendee_email=attendee,
            start_at=payload.start_at,
            end_at=payload.end_at,
            status="scheduled",
            notes=notas,
            google_event_id=google_event_id,
            created_by="panel",
            timezone=await _timezone_name(),
        )
        session.add(cita)
        await session.flush()
        return AppointmentOut(
            id=cita.id,
            title=cita.title,
            attendee_email=cita.attendee_email,
            start_at=cita.start_at,
            end_at=cita.end_at,
            status=cita.status,
            notes=cita.notes,
            google_event_id=cita.google_event_id,
            appointment_type_id=cita.appointment_type_id,
            created_by=cita.created_by,
            timezone=cita.timezone,
        )


# ---------------------------------------------------------------- bloqueos
class BlockIn(BaseModel):
    start_at: datetime
    end_at: datetime
    reason: str = ""


class BlockOut(BaseModel):
    id: uuid.UUID
    start_at: datetime
    end_at: datetime
    reason: str


@router.get("/blocks", response_model=list[BlockOut])
async def list_blocks(
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos"))],
    days: Annotated[int, Query(ge=1, le=365)] = 180,
    include_past: Annotated[bool, Query()] = False,
) -> list[BlockOut]:
    """Bloqueos de la agenda interna (vacaciones, ausencia, feriados, almuerzo)."""
    from core.db.models import utcnow
    from pack_calendar.models import CalendarBlock

    now = utcnow()
    start = now - timedelta(days=1) if include_past else now
    end = now + timedelta(days=days)
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(CalendarBlock)
                    .where(
                        CalendarBlock.tenant_id == principal.tenant_id,
                        CalendarBlock.deleted_at.is_(None),
                        CalendarBlock.end_at >= start,
                        CalendarBlock.start_at <= end,
                    )
                    .order_by(CalendarBlock.start_at)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
    return [
        BlockOut(id=b.id, start_at=b.start_at, end_at=b.end_at, reason=b.reason or "") for b in rows
    ]


@router.post("/blocks", response_model=BlockOut, status_code=201)
async def create_block(
    payload: BlockIn,
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos", write=True))],
) -> BlockOut:
    from pack_calendar.models import CalendarBlock

    if principal.tenant_id is None:
        raise ApiError.bad_request("Sin negocio activo.", "no_tenant")
    if payload.end_at <= payload.start_at:
        raise ApiError.bad_request("El fin debe ser posterior al inicio.", "invalid_range")
    async with get_session() as session:
        block = CalendarBlock(
            tenant_id=principal.tenant_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
            reason=payload.reason.strip(),
        )
        session.add(block)
        await session.flush()
        return BlockOut(
            id=block.id, start_at=block.start_at, end_at=block.end_at, reason=block.reason
        )


@router.delete("/blocks/{block_id}", status_code=204)
async def delete_block(
    block_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos", write=True))],
) -> Response:
    from core.db.models import utcnow
    from pack_calendar.models import CalendarBlock

    async with get_session() as session:
        row = await get_owned(session, CalendarBlock, block_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Bloqueo no encontrado.")
        row.deleted_at = utcnow()
    return Response(status_code=204)


# ---------------------------------------------------------------- vista semanal
class WeekItem(BaseModel):
    kind: str  # "appointment" | "block"
    id: uuid.UUID
    title: str
    start_at: datetime
    end_at: datetime
    status: str
    source: str  # "local" | "google"


class WeekOut(BaseModel):
    start: str
    days: list[dict[str, Any]]
    items: list[WeekItem]


@router.get("/week", response_model=WeekOut)
async def week_view(
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
    start: Annotated[str | None, Query()] = None,
) -> WeekOut:
    """Citas + bloqueos de la semana que arranca en `start` (YYYY-MM-DD, lunes si se omite)."""
    from core.config import get_settings
    from pack_calendar.config import CalendarConfig
    from pack_calendar.models import Appointment, CalendarBlock

    tzname = getattr(get_settings(), "calendar_timezone", None) or "America/Bogota"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except Exception:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Bogota")
    now = datetime.now(UTC)
    if start:
        base = datetime.fromisoformat(start).replace(tzinfo=tz)
    else:
        hoy = now.astimezone(tz)
        base = (hoy - timedelta(days=hoy.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = base + timedelta(days=7)
    start_utc, end_utc = base.astimezone(UTC), end.astimezone(UTC)

    # Horario de atención del negocio (si el pack está configurado).
    try:
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None

    async with get_session() as session:
        citas = (
            (
                await session.execute(
                    select(Appointment)
                    .where(
                        Appointment.tenant_id == principal.tenant_id,
                        Appointment.deleted_at.is_(None),
                        Appointment.start_at < end_utc,
                        Appointment.end_at > start_utc,
                    )
                    .order_by(Appointment.start_at)
                )
            )
            .scalars()
            .all()
        )
        blocks = (
            (
                await session.execute(
                    select(CalendarBlock)
                    .where(
                        CalendarBlock.tenant_id == principal.tenant_id,
                        CalendarBlock.deleted_at.is_(None),
                        CalendarBlock.start_at < end_utc,
                        CalendarBlock.end_at > start_utc,
                    )
                    .order_by(CalendarBlock.start_at)
                )
            )
            .scalars()
            .all()
        )

    items: list[WeekItem] = [
        WeekItem(
            kind="appointment",
            id=c.id,
            title=c.title or "Cita",
            start_at=c.start_at,
            end_at=c.end_at,
            status=c.status,
            source="google" if c.google_event_id else "local",
        )
        for c in citas
    ] + [
        WeekItem(
            kind="block",
            id=b.id,
            title=b.reason or "Bloqueo",
            start_at=b.start_at,
            end_at=b.end_at,
            status="blocked",
            source="local",
        )
        for b in blocks
    ]
    items.sort(key=lambda i: i.start_at)

    from pack_calendar.skills import _open_window

    days = []
    for i in range(7):
        d = base + timedelta(days=i)
        iso = d.strftime("%Y-%m-%d")
        days.append(
            {
                "date": iso,
                "weekday": d.weekday(),
                "business_hours": _open_window(config, iso) if isinstance(config, CalendarConfig) else "",
            }
        )
    return WeekOut(start=base.strftime("%Y-%m-%d"), days=days, items=items)


# ---------------------------------------------------------------- eventos (FullCalendar)
class EventOut(BaseModel):
    id: str
    title: str
    start: str
    end: str
    kind: str  # appointment | block
    status: str
    source: str  # local | google
    color: str
    can_cancel: bool
    attendee_email: str = ""
    notes: str = ""
    appointment_type_id: str = ""
    html_link: str = ""


@router.get("/events")
async def calendar_events(
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
    start: Annotated[str | None, Query()] = None,
    end: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Citas + bloqueos en formato FullCalendar para la grilla del panel (HU-CAL-03).

    `start`/`end` ISO (o YYYY-MM-DD). Sin parámetros, cubre el mes en curso.
    """
    from pack_calendar.models import Appointment, CalendarBlock

    now = datetime.now(UTC)
    def _parse(value: str | None, default: datetime) -> datetime:
        if not value:
            return default
        try:
            return datetime.fromisoformat(value).astimezone(UTC)
        except (ValueError, TypeError):
            return default

    ini = _parse(start, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    fin = _parse(end, ini + timedelta(days=31))
    if fin < ini:
        fin = ini + timedelta(days=31)
    if fin - ini > timedelta(days=400):
        fin = ini + timedelta(days=400)

    async with get_session() as session:
        citas = (await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == principal.tenant_id,
                Appointment.deleted_at.is_(None),
                Appointment.start_at < fin,
                Appointment.end_at > ini,
            ).order_by(Appointment.start_at)
        )).scalars().all()
        blocks = (await session.execute(
            select(CalendarBlock).where(
                CalendarBlock.tenant_id == principal.tenant_id,
                CalendarBlock.deleted_at.is_(None),
                CalendarBlock.start_at < fin,
                CalendarBlock.end_at > ini,
            ).order_by(CalendarBlock.start_at)
        )).scalars().all()

    events: list[dict[str, Any]] = []
    for a in citas:
        events.append({
            "id": f"appt-{a.id}",
            "title": a.title or "Cita",
            "start": a.start_at.astimezone(UTC).isoformat(),
            "end": a.end_at.astimezone(UTC).isoformat(),
            "kind": "appointment",
            "status": a.status,
            "source": "google" if a.google_event_id else "local",
            "color": "#198754" if a.status == "scheduled" else "#6c757d",
            "can_cancel": a.status == "scheduled",
            "attendee_email": a.attendee_email or "",
            "notes": a.notes or "",
            "appointment_type_id": str(a.appointment_type_id) if a.appointment_type_id else "",
        })
    for b in blocks:
        events.append({
            "id": f"block-{b.id}",
            "title": b.reason or "Bloqueo",
            "start": b.start_at.astimezone(UTC).isoformat(),
            "end": b.end_at.astimezone(UTC).isoformat(),
            "kind": "block",
            "status": "blocked",
            "source": "local",
            "color": "#dc3545",
            "can_cancel": True,
            "attendee_email": "",
            "notes": "",
            "appointment_type_id": "",
        })
    return {"events": events, "timezone": await _timezone_name()}


class AppointmentPatch(BaseModel):
    title: str | None = None
    attendee_email: str | None = None
    notes: str | None = None
    appointment_type_id: uuid.UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    notify: bool = True


@router.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
async def patch_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentPatch,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
) -> AppointmentOut:
    """Edita campos/horario de una cita y, si se mueve y `notify`, avisa al cliente
    por correo y WhatsApp y a los colaboradores (HU-CAL-02/05). Valida
    solapamiento en el servidor."""
    from core.db.models import utcnow
    from pack_calendar.models import Appointment
    from pack_calendar.skills import CalendarSkill

    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Cita no encontrada.")
        old_start, old_end = row.start_at, row.end_at

    new_start = payload.start_at or old_start
    new_end = payload.end_at or old_end
    if new_end <= new_start:
        raise ApiError.bad_request("El fin debe ser posterior al inicio.", "invalid_range")

    moved = new_start != old_start or new_end != old_end
    # Validación dura (solo agenda interna): no doble reserva al mover.
    from core.plugin_manager import plugin_manager

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    from pack_calendar.config import CalendarConfig

    if moved and isinstance(config, CalendarConfig) and getattr(config, "source", "interno") != "google":
        conflict = await CalendarSkill()._validate_internal_slot(
            config, new_start, new_end, exclude_appt_id=appointment_id
        )
        if conflict is not None:
            raise ApiError.bad_request(
                "Ese horario ya está ocupado o está fuera del horario de atención.",
                conflict,
            )

    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Cita no encontrada.")
        if payload.title is not None:
            row.title = payload.title.strip() or row.title
        if payload.attendee_email is not None:
            row.attendee_email = payload.attendee_email.strip() or None
        if payload.notes is not None:
            row.notes = payload.notes.strip()
        if payload.appointment_type_id is not None:
            row.appointment_type_id = payload.appointment_type_id
        row.start_at = new_start
        row.end_at = new_end
        row.updated_at = utcnow()
        conversation_id = row.conversation_id
        out = AppointmentOut(
            id=row.id, title=row.title, attendee_email=row.attendee_email,
            start_at=row.start_at, end_at=row.end_at, status=row.status, notes=row.notes,
            google_event_id=row.google_event_id, appointment_type_id=row.appointment_type_id,
            created_by=row.created_by, timezone=row.timezone,
        )

    if moved:
        from pack_calendar.skills import CalendarSkill as _CS

        skill = _CS()
        await skill._record_event(
            action="reprogramada",
            title=out.title,
            appointment_id=appointment_id,
            old_start=old_start,
            new_start=new_start,
            actor="panel",
        )
        if payload.notify:
            out.notification = await _notify_client(
                "moved",
                title=out.title,
                start=new_start,
                end=new_end,
                attendee_email=out.attendee_email,
                conversation_id=conversation_id,
            )
    return out


class ActivityOut(BaseModel):
    action: str
    title: str
    actor: str
    old_start_at: datetime | None
    new_start_at: datetime | None
    created_at: datetime


@router.get("/appointments/{appointment_id}/activities", response_model=list[ActivityOut])
async def appointment_activities(
    appointment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
) -> list[ActivityOut]:
    """Historial de una cita (programada/reprogramada/cancelada) (HU-CAL-03)."""
    from pack_calendar.models import AppointmentEvent

    async with get_session() as session:
        rows = (await session.execute(
            select(AppointmentEvent).where(
                AppointmentEvent.tenant_id == principal.tenant_id,
                AppointmentEvent.appointment_id == appointment_id,
            ).order_by(AppointmentEvent.created_at.desc()).limit(100)
        )).scalars().all()
    return [
        ActivityOut(
            action=e.action, title=e.title, actor=e.actor,
            old_start_at=e.old_start_at, new_start_at=e.new_start_at, created_at=e.created_at,
        )
        for e in rows
    ]


class BlockPatch(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    reason: str | None = None


@router.patch("/blocks/{block_id}", response_model=BlockOut)
async def patch_block(
    block_id: uuid.UUID,
    payload: BlockPatch,
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos", write=True))],
) -> BlockOut:
    """Edita un bloqueo de agenda (arrastrar/redimensionar en la grilla, HU-CAL-03)."""
    from core.db.models import utcnow
    from pack_calendar.models import CalendarBlock

    async with get_session() as session:
        row = await get_owned(session, CalendarBlock, block_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Bloqueo no encontrado.")
        new_start = payload.start_at or row.start_at
        new_end = payload.end_at or row.end_at
        if new_end <= new_start:
            raise ApiError.bad_request("El fin debe ser posterior al inicio.", "invalid_range")
        row.start_at = new_start
        row.end_at = new_end
        if payload.reason is not None:
            row.reason = payload.reason.strip()
        row.updated_at = utcnow()
        return BlockOut(id=row.id, start_at=row.start_at, end_at=row.end_at, reason=row.reason)


def _panel_base() -> str:
    from core.config import get_settings

    base = (get_settings().panel_base_url or get_settings().public_base_url).rstrip("/")
    return base

async def _calendar_config() -> Any:
    """Config del pack (None si no está configurado — p. ej. admin master)."""
    from core.plugin_manager import plugin_manager

    try:
        return await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        return None


async def _timezone_name() -> str:
    """Zona horaria del negocio (fallback `America/Bogota`)."""
    config = await _calendar_config()
    return getattr(config, "timezone", None) or "America/Bogota"


# Cookies del flujo OAuth (HU-CAL-07): el `state` anti-CSRF y el destino de vuelta.
_OAUTH_STATE_COOKIE = "oauth_state"
_OAUTH_NEXT_COOKIE = "oauth_next"
_OAUTH_COOKIE_MAX_AGE = 600


@router.get("/oauth/start", status_code=302)
async def oauth_start(
    principal: Annotated[Principal, Depends(require("pack_calendar:conexion", write=True))],
    next: str = "/calendario",
) -> Response:
    """Inicia el OAuth2 de Google (302 a accounts.google.com).

    Deja dos cookies httponly que el panel reenvía al navegador: `oauth_state`
    (anti-CSRF, D-SPEC-24-12) y `oauth_next` (a dónde volver). El retorno de Google
    entra por la ruta BFF del panel (D-SPEC-24-11, ADR-0093).
    """
    import secrets

    from fastapi.responses import RedirectResponse

    from pack_calendar.oauth import build_authorization_url, panel_redirect_uri

    config = await _calendar_config()
    if config is None or not (getattr(config, "client_id", "") or "").strip():
        raise ApiError.bad_request(
            "Configura las credenciales de Google del pack de Calendario antes de conectar.",
            "not_configured",
        )
    destino = next if next.startswith("/") and not next.startswith("//") else "/calendario"
    state = secrets.token_urlsafe(32)
    respuesta = RedirectResponse(
        build_authorization_url(config.client_id, redirect=panel_redirect_uri(), state=state),
        status_code=302,
    )
    for nombre, valor in ((_OAUTH_STATE_COOKIE, state), (_OAUTH_NEXT_COOKIE, destino)):
        respuesta.set_cookie(
            nombre, valor, max_age=_OAUTH_COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/"
        )
    return respuesta


@router.get("/oauth/callback", status_code=302)
async def oauth_callback(
    request: Request,
    principal: Annotated[Principal, Depends(require("pack_calendar:conexion", write=True))],
    code: str = "",
    state: str = "",
    error: str = "",
) -> Response:
    """Vuelve de Google (vía la ruta BFF del panel): valida el `state`, intercambia
    el código, guarda los tokens en la conexión del negocio y regresa al panel.

    Exige sesión (el BFF reenvía la del usuario): así el negocio es el del
    principal y no se puede guardar una cuenta en otro (HU-CAL-07). Sin `state`
    o con un `state` distinto al de la cookie, no se intercambia nada (CSRF).
    """
    import hmac

    from fastapi.responses import RedirectResponse

    from core.shared.logger import get_logger
    from pack_calendar import oauth

    log = get_logger(__name__)
    marcador = request.cookies.get(_OAUTH_NEXT_COOKIE) or ""
    destino = marcador if marcador.startswith("/") and not marcador.startswith("//") else "/calendario"

    def volver(estado: str) -> RedirectResponse:
        respuesta = RedirectResponse(f"{_panel_base()}{destino}?google={estado}", status_code=302)
        for nombre in (_OAUTH_STATE_COOKIE, _OAUTH_NEXT_COOKIE):
            respuesta.delete_cookie(nombre, path="/")
        return respuesta

    esperado = request.cookies.get(_OAUTH_STATE_COOKIE) or ""
    if not esperado or not state or not hmac.compare_digest(esperado, state):
        log.warning("calendar_oauth_state_invalido", tenant_id=str(principal.tenant_id))
        return volver("error")
    if error or not code:
        return volver("cancelado")
    config = await _calendar_config()
    if config is None:
        return volver("error")
    try:
        tokens = await oauth.exchange_code(
            config.client_id,
            config.client_secret.get_secret_value(),
            code,
            redirect=oauth.panel_redirect_uri(),
        )
        await oauth.save_tokens(tokens)
    except Exception:
        log.exception("calendar_oauth_exchange_failed")
        return volver("error")
    return volver("ok")

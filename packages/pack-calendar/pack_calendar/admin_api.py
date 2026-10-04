"""API admin del pack de Calendario (ADR-0079).

Montada en `/api/v1/pack/calendar`. Estado de la conexión, tipos de cita y citas
(listar y cancelar). La configuración del pack ya se edita con el formulario
genérico de JSON Schema en `/habilidades/pack_calendar`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.session import get_owned, get_session

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
        )
        for a in rows
    ]


@router.post("/appointments/{appointment_id}/cancel", status_code=204)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
) -> Response:
    """Cancela la cita: borra el evento en Google (best-effort) y marca el espejo."""
    from core.db.models import utcnow

    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
    if row is None:
        raise ApiError.not_found("Cita no encontrada.")

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
    return Response(status_code=204)


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
    if config is not None:
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


@router.get("/oauth/start", status_code=302)
async def oauth_start(
    principal: Annotated[Principal, Depends(require("pack_calendar:conexion", write=True))],
    next: str = "/calendario",
) -> Response:
    """Inicia el OAuth2 de Google (302 a accounts.google.com)."""
    from fastapi.responses import RedirectResponse

    from pack_calendar.oauth import api_redirect_uri, build_authorization_url

    config = await _calendar_config()
    if config is None or not (getattr(config, "client_id", "") or "").strip():
        raise ApiError.bad_request(
            "Configura las credenciales de Google del pack de Calendario antes de conectar.",
            "not_configured",
        )
    destino = next if next.startswith("/") else "/calendario"
    respuesta = RedirectResponse(
        build_authorization_url(config.client_id, redirect=api_redirect_uri()), status_code=302
    )
    respuesta.set_cookie("oauth_next", destino, max_age=600, httponly=True, samesite="lax", path="/")
    return respuesta


@router.get("/oauth/callback", status_code=302)
async def oauth_callback(
    request: Request,
    code: str = "",
    error: str = "",
) -> Response:
    """Vuelve de Google: intercambia el código, guarda los tokens y regresa al panel.

    El navegador llega desde Google (sin la cookie de sesión del panel), así que la
    autenticación se apoya en la cookie `oauth_next` que dejó `/oauth/start` (ya
    autenticado): sin ella no se procesa nada.
    """
    from fastapi.responses import RedirectResponse

    from core.shared.logger import get_logger
    from pack_calendar.oauth import api_redirect_uri, exchange_code, save_tokens

    log = get_logger(__name__)
    marcador = request.cookies.get("oauth_next")
    if not marcador:
        # Nadie inició el flujo desde el panel: no se procesa el código.
        return RedirectResponse(f"{_panel_base()}/calendario?google=error", status_code=302)
    destino = marcador if marcador.startswith("/") else "/calendario"

    def volver(estado: str) -> RedirectResponse:
        return RedirectResponse(f"{_panel_base()}{destino}?google={estado}", status_code=302)

    if error or not code:
        return volver("cancelado")
    config = await _calendar_config()
    if config is None:
        return volver("error")
    try:
        tokens = await exchange_code(
            config.client_id,
            config.client_secret.get_secret_value(),
            code,
            redirect=api_redirect_uri(),
        )
        await save_tokens(tokens)
    except Exception:
        log.exception("calendar_oauth_exchange_failed")
        return volver("error")
    return volver("ok")

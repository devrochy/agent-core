"""API admin del pack de Calendario (ADR-0079).

Montada en `/api/v1/pack/calendar`. Estado de la conexión, tipos de cita y citas
(listar y cancelar). La configuración del pack ya se edita con el formulario
genérico de JSON Schema en `/habilidades/pack_calendar`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
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

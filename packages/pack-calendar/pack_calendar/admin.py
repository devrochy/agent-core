"""Panel propio del pack-calendar (`/calendar/…`) — ADR-0061/0069.

Submenú al habilitar el pack: Conexión · Horarios y calendario · Tipos de cita ·
Citas. Reutiliza `base_context` del core y el guard de sección por sub-sección
(ADR-0063).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from core.admin.context import base_context
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

PLUGIN_ID = "pack_calendar"
SECTION = PLUGIN_ID

router = APIRouter(prefix="/calendar", tags=[PLUGIN_ID])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _core_templates_dir() -> Path:
    import core

    return Path(core.__file__).resolve().parent / "admin" / "templates"


_templates.env.loader.searchpath.append(str(_core_templates_dir()))


def _render(name: str, ctx: dict[str, Any]):
    return _templates.TemplateResponse(ctx["request"], name, ctx)


async def _guard(request: Request, section: str, *, write: bool) -> bool:
    from core.admin.permissions import require_section

    return await require_section(request, section, write=write)


def _request_section(request: Request) -> str:
    """Sub-sección del path (ADR-0063); fallback al `plugin_id`."""
    from core.admin.permissions import pack_prefixes, required_section
    from core.plugin_manager import plugin_manager

    req = required_section(
        request.url.path, request.method, pack_prefixes(plugin_manager.list_modules())
    )
    return req[0] if req else SECTION


async def _pack_enabled() -> bool:
    from core.agents.spec import enabled_packs, load_active_spec

    try:
        return PLUGIN_ID in enabled_packs(await load_active_spec())
    except Exception:
        return False


async def _authorized(request: Request, *, write: bool) -> bool:
    return await _guard(request, _request_section(request), write=write) and await _pack_enabled()


# ------------------------------------------------------------------ conexión
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def calendar_home(request: Request):
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin", status_code=303)
    from core.gateway.connections import get_active_connection

    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)

    connection = await get_active_connection("google_calendar")
    ctx.update(
        connected=connection is not None,
        account_email=(connection.external_id if connection else ""),
        connection_label=(connection.label if connection else ""),
    )
    return _render("calendar/index.html", ctx)


# -------------------------------------------------------- horarios/config
@router.get("/configuracion", response_class=HTMLResponse)
async def calendar_config(request: Request):
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin", status_code=303)
    from core.plugin_manager import plugin_manager

    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    ctx.update(
        config={
            "client_id": getattr(config, "client_id", "") or "",
            "has_secret": bool(getattr(config, "client_secret", None)),
            "calendar_id": getattr(config, "calendar_id", "primary"),
            "timezone": getattr(config, "timezone", "America/Bogota"),
            "event_duration_minutes": getattr(config, "event_duration_minutes", 60),
            "slot_minutes": getattr(config, "slot_minutes", 60),
            "buffer_minutes": getattr(config, "buffer_minutes", 0),
            "business_hours": getattr(config, "business_hours", ""),
        }
    )
    return _render("calendar/configuracion.html", ctx)


@router.post("/configuracion")
async def calendar_config_save(request: Request):
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin", status_code=303)
    from core.plugin_manager import plugin_manager

    form = await request.form()
    values: dict[str, Any] = {
        "client_id": str(form.get("client_id", "")),
        "calendar_id": str(form.get("calendar_id", "primary") or "primary"),
        "timezone": str(form.get("timezone", "America/Bogota") or "America/Bogota"),
        "event_duration_minutes": int(form.get("event_duration_minutes", 60) or 60),
        "slot_minutes": int(form.get("slot_minutes", 60) or 60),
        "buffer_minutes": int(form.get("buffer_minutes", 0) or 0),
        "business_hours": str(form.get("business_hours", "") or ""),
    }
    # Horario: JSON válido o rechazo (evita guardar basura que rompería la disponibilidad).
    try:
        json.loads(values["business_hours"] or "{}")
    except json.JSONDecodeError:
        return RedirectResponse(
            "/calendar/configuracion?flash=El horario debe ser JSON válido&flash_type=danger",
            status_code=303,
        )

    secret = str(form.get("client_secret", "") or "")
    try:
        if secret:
            values["client_secret"] = secret
        else:
            existing = await plugin_manager.load_config(PLUGIN_ID)
            values["client_secret"] = existing.client_secret.get_secret_value()  # type: ignore[attr-defined]
        await plugin_manager.save_config(PLUGIN_ID, values)
    except Exception as exc:
        log.exception("calendar_config_save_failed")
        return RedirectResponse(
            f"/calendar/configuracion?flash=Error: {str(exc)[:120]}&flash_type=danger",
            status_code=303,
        )
    return RedirectResponse("/calendar/configuracion?flash=Configuración guardada ✓", status_code=303)


# ------------------------------------------------------------ tipos de cita
@router.get("/tipos", response_class=HTMLResponse)
async def calendar_types(request: Request):
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin", status_code=303)
    from pack_calendar.models import AppointmentType

    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)

    async with get_session() as session:
        types = (
            await session.execute(
                select(AppointmentType)
                .where(
                    AppointmentType.tenant_id == get_current_tenant_id(),
                    AppointmentType.deleted_at.is_(None),
                )
                .order_by(AppointmentType.name)
            )
        ).scalars().all()
    ctx.update(types=types)
    return _render("calendar/tipos.html", ctx)


@router.post("/tipos/guardar")
async def calendar_types_save(request: Request):
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin", status_code=303)
    from pack_calendar.models import AppointmentType

    form = await request.form()
    name = str(form.get("name", "") or "").strip()
    if not name:
        return RedirectResponse("/calendar/tipos?flash=El nombre es obligatorio&flash_type=danger", 303)
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return RedirectResponse("/calendar/tipos?flash=Sin tenant activo&flash_type=danger", 303)
    async with get_session() as session:
        session.add(
            AppointmentType(
                tenant_id=tenant_id,
                name=name,
                duration_minutes=int(form.get("duration_minutes", 60) or 60),
                description=str(form.get("description", "") or ""),
            )
        )
    return RedirectResponse("/calendar/tipos?flash=Tipo de cita creado ✓", status_code=303)


@router.post("/tipos/{type_id}/eliminar")
async def calendar_types_delete(request: Request, type_id: uuid.UUID):
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin", status_code=303)
    from core.db.models import utcnow
    from core.db.session import get_owned
    from pack_calendar.models import AppointmentType

    async with get_session() as session:
        row = await get_owned(session, AppointmentType, type_id, get_current_tenant_id())
        if row is not None:
            row.deleted_at = utcnow()
            row.active = False
    return RedirectResponse("/calendar/tipos?flash=Tipo eliminado", status_code=303)


# ------------------------------------------------------------------- citas
@router.get("/citas", response_class=HTMLResponse)
async def calendar_appointments(request: Request):
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin", status_code=303)
    from pack_calendar.models import Appointment

    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Appointment)
                .where(
                    Appointment.tenant_id == get_current_tenant_id(),
                    Appointment.deleted_at.is_(None),
                )
                .order_by(Appointment.start_at.desc())
                .limit(200)
            )
        ).scalars().all()
    ctx.update(appointments=rows)
    return _render("calendar/citas.html", ctx)


@router.post("/citas/{appointment_id}/cancelar")
async def calendar_appointment_cancel(request: Request, appointment_id: uuid.UUID):
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin", status_code=303)
    from core.db.models import utcnow
    from core.db.session import get_owned
    from pack_calendar.models import Appointment

    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, get_current_tenant_id())
    if row is None:
        return RedirectResponse("/calendar/citas?flash=Cita no encontrada&flash_type=danger", 303)

    # Cancelar en Google (best-effort): el espejo local manda para el panel.
    if row.google_event_id:
        try:
            from core.plugin_manager import plugin_manager
            from pack_calendar.oauth import get_access_token

            config = await plugin_manager.load_config(PLUGIN_ID)
            token = await get_access_token(
                config.client_id, config.client_secret.get_secret_value()  # type: ignore[attr-defined]
            )
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                await client.delete(
                    f"https://www.googleapis.com/calendar/v3/calendars/"
                    f"{config.calendar_id}/events/{row.google_event_id}",  # type: ignore[attr-defined]
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception:
            log.warning("calendar_google_cancel_failed", appointment_id=str(appointment_id))

    async with get_session() as session:
        row_db = await get_owned(session, Appointment, appointment_id, get_current_tenant_id())
        if row_db is not None:
            row_db.status = "cancelled"
            row_db.updated_at = utcnow()
    return RedirectResponse("/calendar/citas?flash=Cita cancelada ✓", status_code=303)

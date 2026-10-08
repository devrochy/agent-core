"""CalendarSkill: agendamiento con **agenda interna** o **Google Calendar**.

Modo (ADR-0084, `CalendarConfig.source`):
- `interno` (default): la agenda vive en `pack_calendar_appointments` +
  `pack_calendar_blocks`; la disponibilidad se calcula contra los horarios de
  atención (`business_hours`). No requiere Google.
- `google`: crea/mueve/borra el evento en Google Calendar y lo espeja local.

En ambos modos `check_availability` devuelve `free_slots` calculados (no solo los
ocupados), para que el asistente no invente horarios. Nunca ofrece fechas ni horas
pasadas (HU-CAL-05): una fecha anterior a hoy devuelve `in_past` y
`find_next_available` propone los próximos días con cupo.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.exceptions import PluginError
from core.shared.tenant import get_current_tenant_id
from pack_calendar.config import CalendarConfig
from pack_calendar.oauth import get_access_token

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
PLUGIN_ID = "pack_calendar"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SOURCE_TTL_SECONDS = 60.0
# Nombres para los datos que devuelven las tools (no son instrucciones al modelo).
_WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTH_NAMES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
)
# `find_next_available`: rango de búsqueda y tope de días con cupo devueltos.
_NEXT_AVAILABLE_MAX_DAYS = 14
_NEXT_AVAILABLE_MAX_RESULTS = 5


def _long_date(value: date) -> str:
    """"miércoles 7 de octubre de 2026" (dato legible para la respuesta de la tool)."""
    return f"{_WEEKDAY_NAMES[value.weekday()]} {value.day} de {_MONTH_NAMES[value.month - 1]} de {value.year}"


def _local_when(value: datetime, tz: ZoneInfo) -> dict[str, str]:
    """Cuándo es una cita, en la zona del negocio y con la fecha escrita.

    E2E HU-CAL-06: la tool devolvía el inicio en UTC y el modelo dijo "7 de
    octubre" para una cita del jueves 8; con el dato ya resuelto no hace cuentas.
    """
    local = value.astimezone(tz)
    return {
        "start": local.isoformat(),
        "fecha": _long_date(local.date()),
        "hora": local.strftime("%H:%M"),
    }


class _KeepUnknown(dict):
    """Deja literal un placeholder que no existe en vez de lanzar KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_template(template: str, **values: str) -> str:
    """Rellena una plantilla configurada por el negocio (correo o WhatsApp).

    Un placeholder mal escrito (`{fechaa}`) queda literal y llaves mal
    balanceadas devuelven la plantilla tal cual: un error de configuración
    nunca debe tumbar el aviso al cliente (HU-CAL-05).
    """
    try:
        return template.format_map(_KeepUnknown(values))
    except (ValueError, IndexError):
        return template


def _calendar_link(start: datetime, end: datetime, title: str, details: str, tz_name: str) -> str:
    """Link "agregar a mi calendario" (plantilla de Google Calendar, D-SPEC-24-9).

    Las fechas van en hora local de la zona del negocio y `ctz` le dice a Google
    cómo interpretarlas. Sirve en WhatsApp y en el correo sin infraestructura extra.
    """
    tz = ZoneInfo(tz_name)
    fmt = "%Y%m%dT%H%M%S"
    query = urlencode(
        {
            "action": "TEMPLATE",
            "text": title,
            "dates": f"{start.astimezone(tz).strftime(fmt)}/{end.astimezone(tz).strftime(fmt)}",
            "ctz": tz_name,
            "details": details,
        }
    )
    return f"https://calendar.google.com/calendar/render?{query}"


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def _google_patch_event(
    config: CalendarConfig, token: str, google_event_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.patch(
            f"{CALENDAR_API}/calendars/{config.calendar_id}/events/{google_event_id}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise PluginError(PLUGIN_ID, f"Calendar API {response.status_code}")
    return response.json()


async def _google_delete_event(config: CalendarConfig, token: str, google_event_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(
            f"{CALENDAR_API}/calendars/{config.calendar_id}/events/{google_event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise PluginError(PLUGIN_ID, f"Calendar API {response.status_code}")


def _open_window(config: CalendarConfig, date: str) -> str:
    """Ventana de atención ("HH:MM-HH:MM") del día, o "" si está cerrado."""
    try:
        parsed = json.loads(config.business_hours or "{}")
        weekday = datetime.fromisoformat(date).weekday()
        return str(parsed.get(WEEKDAYS[weekday], "") or "")
    except (ValueError, json.JSONDecodeError):
        return ""


def _free_slots(
    window: str,
    intervals: list[tuple[datetime, datetime]],
    tz: ZoneInfo,
    date: str,
    *,
    slot_minutes: int,
    duration_minutes: int,
    buffer_minutes: int,
    limit: int = 12,
    not_before: datetime | None = None,
) -> list[str]:
    """Horarios libres ("HH:MM") del día: ventana − ocupados − margen.

    Determinista y puro (testeable): evita que el LLM razone los huecos.
    `not_before` (ahora, si el día es hoy) descarta los horarios que ya empezaron.
    """
    if not window or "-" not in window:
        return []
    start_s, _, end_s = window.partition("-")
    try:
        open_dt = datetime.fromisoformat(f"{date}T{start_s.strip()}:00").replace(tzinfo=tz)
        close_dt = datetime.fromisoformat(f"{date}T{end_s.strip()}:00").replace(tzinfo=tz)
    except ValueError:
        return []
    slot = timedelta(minutes=max(int(slot_minutes), 1))
    duration = timedelta(minutes=max(int(duration_minutes), 1))
    buffer = timedelta(minutes=max(int(buffer_minutes), 0))
    out: list[str] = []
    cursor = open_dt
    while cursor + duration <= close_dt:
        if not_before is not None and cursor <= not_before:
            cursor += slot
            continue
        cand_start = cursor - buffer
        cand_end = cursor + duration + buffer
        if not any(cand_start < b_end and b_start < cand_end for (b_start, b_end) in intervals):
            out.append(cursor.astimezone(tz).strftime("%H:%M"))
            if len(out) >= limit:
                break
        cursor += slot
    return out


def _slot_conflict(
    window: str,
    intervals: list[tuple[datetime, datetime]],
    tz: ZoneInfo,
    start: datetime,
    end: datetime,
    *,
    buffer_minutes: int,
) -> str | None:
    """Validación DURA de un horario concreto (HU-CAL-02). Devuelve el motivo del
    rechazo o None si el horario es válido.

    - `in_past`: el inicio ya pasó.
    - `outside_hours`: fuera de la ventana de atención del día (o día cerrado).
    - `occupied`: se solapa con una cita/bloqueo (el margen se aplica a los ocupados).

    Puro y determinista (testeable sin BD). Al reagendar, el llamador debe pasar
    `intervals` SIN la propia cita (para que no choque consigo misma).
    """
    if start <= datetime.now(tz):
        return "in_past"
    if not window or "-" not in window:
        return "outside_hours"
    date = start.astimezone(tz).strftime("%Y-%m-%d")
    start_s, _, end_s = window.partition("-")
    try:
        open_dt = datetime.fromisoformat(f"{date}T{start_s.strip()}:00").replace(tzinfo=tz)
        close_dt = datetime.fromisoformat(f"{date}T{end_s.strip()}:00").replace(tzinfo=tz)
    except ValueError:
        return "outside_hours"
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    if local_start < open_dt or local_end > close_dt:
        return "outside_hours"
    buffer = timedelta(minutes=max(int(buffer_minutes), 0))
    cand_start = start - buffer
    cand_end = end + buffer
    for b_start, b_end in intervals:
        if cand_start < b_end and b_start < cand_end:
            return "occupied"
    return None


class CalendarSkill(BaseSkill):
    name = "calendar_skill"
    description = "Agendamiento de citas (agenda interna o Google Calendar)"
    keywords = ["cita", "agendar", "reservar", "reunión", "disponibilidad", "horario", "calendario"]

    def __init__(self) -> None:
        # Cache corto de la fuente por tenant: el predicado de conexión se evalúa
        # por tool y por turno; sin esto cargaríamos la config N veces por turno.
        self._source_cache: dict[uuid.UUID, tuple[float, str]] = {}

    async def _tenant_config(self) -> CalendarConfig:
        """Config del pack para el tenant ACTUAL (no una capturada en activación).

        Sin esto, el skill quedaría con el calendario de un solo tenant mientras
        los tokens se resuelven por tenant: una fuga cruzada. Carga por invocación.
        """
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
        if config is None:
            raise PluginError(PLUGIN_ID, "Agenda no configurada para este tenant")
        return config

    async def _timezone_name(self) -> str:
        """Zona horaria del negocio para grabarla en la cita (HU-CAL-02).

        Fail-open a `America/Bogota` si el pack no está configurado: la cita debe
        poder guardarse aunque la config no cargue.
        """
        try:
            return (await self._tenant_config()).timezone or "America/Bogota"
        except Exception:
            return "America/Bogota"

    async def _email_provided_by_client(self, email: str, context: ToolContext) -> bool:
        """True si el email lo dio el CLIENTE (mensaje inbound) o está en su perfil.

        Corrección post-prueba 2026-10-05: el modelo inventó `rob@example.com`
        para satisfacer el requisito de email y la cita se creó con un correo
        falso. El email solo se acepta si aparece en los mensajes del cliente o
        en `Customer.attributes`. Sin conversación (p. ej. playground sin
        historial) no se puede verificar: se acepta el email ya validado.
        """
        conv_id = _as_uuid(getattr(context, "conversation_id", None))
        if conv_id is None:
            return True
        from sqlalchemy import Text, cast, select

        from core.db.models import Customer, Message
        from core.db.session import get_session

        tenant_id = get_current_tenant_id()
        pattern = f"%{email}%"
        async with get_session() as session:
            hit = (
                await session.execute(
                    select(Message.id)
                    .where(
                        Message.tenant_id == tenant_id,
                        Message.conversation_id == conv_id,
                        Message.direction == "inbound",
                        Message.deleted_at.is_(None),
                        Message.content.ilike(pattern),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if hit is not None:
                return True
            cust_id = _as_uuid(getattr(context, "customer_id", None))
            q = select(Customer.id).where(
                Customer.tenant_id == tenant_id,
                cast(Customer.attributes, Text).ilike(pattern),
            )
            if cust_id is not None:
                q = q.where(Customer.id == cust_id)
            return (await session.execute(q.limit(1))).scalar_one_or_none() is not None

    async def _business_name(self) -> str:
        """Nombre visible del negocio (nunca un nombre técnico, HU-REP-04)."""
        tenant_id = get_current_tenant_id()
        name = ""
        if tenant_id is not None:
            try:
                from sqlalchemy import select

                from core.db.models import AppSettings
                from core.db.session import get_session

                async with get_session() as session:
                    row = (
                        await session.execute(
                            select(AppSettings.assistant_name).where(
                                AppSettings.tenant_id == tenant_id
                            )
                        )
                    ).scalar_one_or_none()
                    name = (row or "").strip()
            except Exception:
                name = ""
        return name or "SolversHub"

    async def _source(self) -> str:
        tenant_id = get_current_tenant_id()
        now = time.monotonic()
        if tenant_id is not None:
            cached = self._source_cache.get(tenant_id)
            if cached is not None and now - cached[0] < _SOURCE_TTL_SECONDS:
                return cached[1]
        config = await self._tenant_config()
        source = str(getattr(config, "source", "interno") or "interno")
        if tenant_id is not None:
            self._source_cache[tenant_id] = (now, source)
        return source

    async def _connections_ok(self, connected: set[str]) -> bool:
        """Gating condicional (ADR-0084): interno no requiere Google; google sí."""
        if await self._source() == "google":
            return "google_calendar" in connected
        return True

    async def _google_token(self, config: CalendarConfig) -> str:
        return await get_access_token(config.client_id, config.client_secret.get_secret_value())

    async def _appointment_type(self, value: Any) -> Any | None:
        """Tipo de cita activo por id (o None si no viene / no existe)."""
        type_id = _as_uuid(value)
        if type_id is None:
            return None
        from core.db.session import get_session
        from pack_calendar.models import AppointmentType

        tenant_id = get_current_tenant_id()
        async with get_session() as session:
            from sqlalchemy import select

            return (
                await session.execute(
                    select(AppointmentType).where(
                        AppointmentType.id == type_id,
                        AppointmentType.tenant_id == tenant_id,
                        AppointmentType.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

    def get_tools(self) -> list[MCPTool]:
        # Todas las tools usan `connection_predicate` (no `requires_connection_types`)
        # porque el requisito de Google depende de la config del tenant (modo).
        return [
            MCPTool(
                name="create_calendar_event",
                description=(
                    "Crea una cita en la agenda del negocio. Úsala SOLO cuando el cliente "
                    "haya confirmado fecha y hora exactas. Fechas en formato ISO: 2026-08-20T15:00:00. "
                    "El email del cliente es OBLIGATORIO y debe ser uno que el cliente haya dado "
                    "en la conversación (o que ya esté en su perfil): NUNCA lo inventes. Si no lo "
                    "tenés, pedíselo antes de llamar esta tool (sin email la tool devuelve "
                    "needs_data y no crea nada). "
                    "Si sabés el tipo de cita (ej. una demo), pasá appointment_type_id; si no, "
                    "pasá duration_minutes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Título, ej: 'Demo con Carlos Ruiz'"},
                        "start_datetime": {"type": "string", "description": "Inicio ISO 8601 (hora local del negocio)"},
                        "description": {"type": "string", "description": "Detalle de la cita"},
                        "attendee_email": {"type": "string", "description": "Email del cliente (obligatorio; pedíselo si no lo tenés)"},
                        "appointment_type_id": {"type": "string", "description": "ID del tipo de cita (opcional)"},
                        "duration_minutes": {"type": "integer", "description": "Duración en minutos (opcional; si no, la del tipo o la configurada)"},
                    },
                    "required": ["title", "start_datetime"],
                },
                handler=self.create_event,
                connection_predicate=self._connections_ok,
            ),
            MCPTool(
                name="check_availability",
                description=(
                    "Consulta la disponibilidad de una fecha y devuelve los horarios LIBRES "
                    "reales (free_slots). Úsala SIEMPRE ANTES de proponer o crear una cita: "
                    "ofrecé al cliente solo horarios de free_slots, nunca inventes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Fecha a consultar, formato YYYY-MM-DD"},
                        "duration_minutes": {"type": "integer", "description": "Duración deseada en minutos (opcional)"},
                    },
                    "required": ["date"],
                },
                handler=self.check_availability,
                connection_predicate=self._connections_ok,
            ),
            MCPTool(
                name="find_next_available",
                description=(
                    "Próximos días con horarios libres desde hoy. Usala cuando el cliente no da "
                    "fecha o pide lo más pronto posible. No la uses si el cliente ya dijo un día: "
                    "para eso usá check_availability."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _NEXT_AVAILABLE_MAX_DAYS,
                            "description": "Cuántos días hacia adelante buscar (1-14, por defecto 7)",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Duración deseada en minutos (opcional)",
                        },
                    },
                    "required": [],
                },
                handler=self.find_next_available,
                connection_predicate=self._connections_ok,
            ),
            MCPTool(
                name="update_calendar_event",
                description=(
                    "Reagenda/reprograma una cita YA EXISTENTE a una nueva fecha/hora (la MUEVE, "
                    "no crea otra). REGLA OBLIGATORIA: cuando el cliente pide reagendar/mover una "
                    "cita ya agendada, NUNCA uses create_calendar_event: usá SIEMPRE esta tool. "
                    "Úsala SOLO cuando el cliente haya confirmado la nueva fecha y hora exactas. "
                    "Pasá el event_id que devolvió create_calendar_event/list_client_appointments; "
                    "si no, el sistema busca la próxima cita del cliente por título o email. "
                    "NUNCA inventes un event_id: si no estás seguro, omitilo (el sistema busca por "
                    "la conversación). Fechas ISO 8601 en hora local del negocio."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "start_datetime": {
                            "type": "string",
                            "description": "Nueva fecha/hora ISO 8601 (hora local del negocio), ej: 2026-09-30T14:00:00",
                        },
                        "event_id": {"type": "string", "description": "ID del evento/cita. Opcional."},
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Nueva duración en minutos (opcional; por defecto conserva la actual)",
                        },
                        "title": {
                            "type": "string",
                            "description": "Título de la cita para localizarla si no pasás event_id (opcional)",
                        },
                        "attendee_email": {
                            "type": "string",
                            "description": "Email del cliente para localizarla si no pasás event_id (opcional)",
                        },
                    },
                    "required": ["start_datetime"],
                },
                handler=self.update_event,
                connection_predicate=self._connections_ok,
            ),
            MCPTool(
                name="list_client_appointments",
                description=(
                    "Lista las citas AGENDADAS (scheduled) del cliente, ordenadas por fecha, con un "
                    "índice numerado y su event_id. Úsala cuando el cliente pregunte 'qué citas tengo' "
                    "o quiera cancelar VARIAS o TODAS sus citas: mostrá la lista numerada al cliente "
                    "y pedile que elija por número o 'todas'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "attendee_email": {
                            "type": "string",
                            "description": "Email del cliente (opcional; si no se pasa, usa la conversación actual)",
                        },
                    },
                    "required": [],
                },
                handler=self.list_appointments,
                connection_predicate=self._connections_ok,
            ),
            MCPTool(
                name="cancel_calendar_event",
                description=(
                    "Cancela una o varias citas YA EXISTENTES. REGLA OBLIGATORIA: cuando el cliente "
                    "pide cancelar su cita/demo, NUNCA escales a un humano ni digas 'paso tu solicitud': "
                    "usá SIEMPRE esta tool. Para cancelar VARIAS o TODAS, primero llamá "
                    "list_client_appointments y usá los event_ids que devuelve, o pasá cancel_all=true. "
                    "Úsala SOLO cuando el cliente pidió cancelar explícitamente y confirmó. Después, "
                    "comunicá EXACTAMENTE lo que devolvió la tool (status 'cancelled' o 'not_found'). "
                    "NUNCA inventes un event_id: si no estás seguro, omitilo (el sistema busca por "
                    "la conversación)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "ID del evento/cita (opcional)"},
                        "event_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de event_ids a cancelar (de list_client_appointments) (opcional)",
                        },
                        "cancel_all": {
                            "type": "boolean",
                            "description": "Si es true, cancela TODAS las citas agendadas con el email del cliente (opcional)",
                        },
                        "title": {"type": "string", "description": "Título de la cita para localizarla (opcional)"},
                        "attendee_email": {"type": "string", "description": "Email del cliente para localizarla (opcional)"},
                    },
                    "required": [],
                },
                handler=self.cancel_event,
                connection_predicate=self._connections_ok,
            ),
            MCPTool(
                name="list_appointment_types",
                description=(
                    "Lista los tipos de cita/servicio configurados (nombre, duración y id). Úsala "
                    "antes de agendar si el negocio ofrece varios servicios (p. ej. 'demo', "
                    "'asesoría') para elegir el tipo correcto."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.list_appointment_types,
                connection_predicate=self._connections_ok,
            ),
        ]

    async def list_appointment_types(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        from sqlalchemy import select

        from core.db.session import get_session
        from pack_calendar.models import AppointmentType

        tenant_id = get_current_tenant_id()
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(AppointmentType)
                    .where(
                        AppointmentType.tenant_id == tenant_id,
                        AppointmentType.active.is_(True),
                        AppointmentType.deleted_at.is_(None),
                    )
                    .order_by(AppointmentType.name.asc())
                )
            ).scalars().all()
        return {
            "types": [
                {
                    "appointment_type_id": str(r.id),
                    "name": r.name,
                    "duration_minutes": r.duration_minutes,
                }
                for r in rows
            ],
            "total": len(rows),
        }

    # ------------------------------------------------------------ handlers
    async def create_event(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        source = await self._source()
        config = await self._tenant_config()
        tz = ZoneInfo(config.timezone)
        start = datetime.fromisoformat(str(arguments["start_datetime"]))
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)

        appt_type = await self._appointment_type(arguments.get("appointment_type_id"))
        duration = int(arguments.get("duration_minutes") or 0)
        if duration <= 0:
            duration = int(getattr(appt_type, "duration_minutes", 0) or 0) or config.event_duration_minutes
        end = start + timedelta(minutes=duration)
        title = str(arguments.get("title") or (getattr(appt_type, "name", "") or "Cita"))

        # Email OBLIGATORIO (corrección post-prueba 2026-10-05): sin email no hay
        # confirmación posible. La tool NO crea y pide el dato; el `error` hace que
        # el guardrail no cuente el intento como respaldo de "quedó agendada".
        from core.gateway.channels.email import single_recipient

        email = str(arguments.get("attendee_email") or "").strip()
        if not email:
            return {
                "error": "Falta el email del cliente: pedíselo antes de agendar (es obligatorio).",
                "status": "needs_data",
                "missing": ["attendee_email"],
            }
        if single_recipient(email) is None:
            return {
                "error": "El email del cliente no es válido: pedíselo de nuevo.",
                "status": "needs_data",
                "missing": ["attendee_email"],
            }
        if not await self._email_provided_by_client(email, context):
            # El modelo inventó el email (probado 2026-10-05): solo se acepta el
            # que el cliente escribió o el que está en su perfil.
            return {
                "error": (
                    "Ese email no me lo dio el cliente: pedíselo antes de agendar "
                    "y NO inventes emails."
                ),
                "status": "needs_data",
                "missing": ["attendee_email"],
            }

        # Validación DURA en el servidor (HU-CAL-02): no confiar en que el LLM
        # haya llamado antes a check_availability. Solo agenda interna; en modo
        # google manda la disponibilidad de Google.
        if source != "google":
            conflict = await self._validate_internal_slot(config, start, end)
            if conflict is not None:
                return {
                    "status": "rejected",
                    "reason": conflict,
                    "message": {
                        "occupied": "Ese horario ya está ocupado. Ofrecé otro de free_slots.",
                        "outside_hours": "Ese horario está fuera del horario de atención.",
                        "in_past": "Esa fecha/hora ya pasó.",
                        "invalid_range": "El fin debe ser posterior al inicio.",
                    }.get(conflict, "Horario inválido."),
                }

        google_event_id: str | None = None
        business = await self._business_name()
        # Link "agregar a mi calendario" (D-SPEC-24-9): en modo interno es el que
        # ve el cliente; en ambos modos es el que va en el correo.
        calendar_link = _calendar_link(start, end, title, f"Cita agendada con {business}", config.timezone)
        link = calendar_link
        if source == "google":
            token = await self._google_token(config)
            body: dict[str, Any] = {
                "summary": title,
                "description": str(arguments.get("description", ""))
                + f"\nCita agendada con {business}",
                "start": {"dateTime": start.isoformat(), "timeZone": config.timezone},
                "end": {"dateTime": end.isoformat(), "timeZone": config.timezone},
            }
            if arguments.get("attendee_email"):
                body["attendees"] = [{"email": str(arguments["attendee_email"])}]
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{CALENDAR_API}/calendars/{config.calendar_id}/events",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code >= 400:
                raise PluginError(PLUGIN_ID, f"Calendar API {response.status_code}")
            event = response.json()
            google_event_id = event.get("id")
            link = event.get("htmlLink", "") or calendar_link

        from pack_calendar.demo import HorarioOcupado, datos_de_cita, enlace_de, texto_confirmacion

        try:
            appt_id = await self._mirror_appointment(
                arguments,
                context,
                start,
                end,
                google_event_id=google_event_id,
                appointment_type_id=getattr(appt_type, "id", None),
                title=title,
                asignado_admin_id=getattr(appt_type, "responsable_admin_id", None),
            )
        except HorarioOcupado:
            # HU-SH-02: dos prospectos eligieron el mismo horario a la vez; este perdió.
            dia = start.astimezone(tz).date().isoformat()
            alternativas = await self.check_availability({"date": dia, "duration_minutes": duration}, context)
            return {
                "status": "rejected",
                "reason": "occupied",
                "message": "Ese horario acaba de ocuparse. Ofrecé uno de free_slots.",
                "free_slots": alternativas.get("free_slots", []),
            }
        await self._record_event(
            action="programada",
            title=title,
            appointment_id=appt_id,
            conversation_id=context.conversation_id,
            new_start=start,
        )
        email_sent, email_reason = await self._notify_appointment_email(
            "confirmed",
            title=title,
            start=start,
            attendee_email=(
                str(arguments["attendee_email"]) if arguments.get("attendee_email") else None
            ),
            config=config,
            link=calendar_link,
        )
        return {
            "event_id": google_event_id or (str(appt_id) if appt_id else None),
            "status": "created",
            **_local_when(start, tz),
            "email_sent": email_sent,
            "email_reason": email_reason,
            # SafeRenderer: la confirmación exacta (fecha, hora, zona y enlace de la reunión,
            # HU-SH-02) la adjunta el core; el modelo no la redacta.
            "_safe_append": texto_confirmacion(
                config.confirmacion_texto,
                datos_de_cita(title, start, config.timezone, enlace_de(appt_type, config)),
            )
            + (f"\n🗓️ Agregala a tu calendario: {link}" if link else ""),
        }

    async def _mirror_appointment(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
        start: datetime,
        end: datetime,
        *,
        google_event_id: str | None = None,
        appointment_type_id: uuid.UUID | None = None,
        title: str = "",
        asignado_admin_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Espeja la cita en la tabla del pack (fuente de verdad en modo interno).

        Devuelve el `id` de la fila espejo (para el historial) o `None` si no se pudo.
        """
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            from core.shared.logger import get_logger

            get_logger(__name__).warning("calendar_mirror_sin_tenant")
            return None
        try:
            from core.db.session import get_session
            from pack_calendar.models import Appointment

            async with get_session() as session:
                row = Appointment(
                    tenant_id=tenant_id,
                    conversation_id=_as_uuid(context.conversation_id),
                    customer_id=_as_uuid(context.customer_id),
                    appointment_type_id=appointment_type_id,
                    google_event_id=google_event_id,
                    title=title or str(arguments.get("title", "Cita")),
                    attendee_email=(
                        str(arguments["attendee_email"])
                        if arguments.get("attendee_email")
                        else None
                    ),
                    start_at=start,
                    end_at=end,
                    status="scheduled",
                    created_by="assistant",
                    timezone=await self._timezone_name(),
                    asignado_admin_id=asignado_admin_id,
                )
                session.add(row)
                await session.flush()
                return row.id
        except Exception as exc:  # nunca tumbar el agendamiento real por el espejo
            from pack_calendar.demo import HorarioOcupado, es_solape

            if es_solape(exc):
                # En modo interno la fila ES la cita: otra reserva ganó el horario.
                raise HorarioOcupado from exc
            from core.shared.logger import get_logger

            get_logger(__name__).warning("calendar_mirror_failed", exc_info=True)
            return None

    async def _record_event(
        self,
        *,
        action: str,
        title: str = "",
        appointment_id: uuid.UUID | None = None,
        conversation_id: Any = None,
        old_start: datetime | None = None,
        new_start: datetime | None = None,
        actor: str = "asistente",
    ) -> None:
        """Registra una acción del historial de la cita (best-effort)."""
        from pack_calendar.models import AppointmentEvent

        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return
        try:
            from core.db.session import get_session

            async with get_session() as session:
                evento = AppointmentEvent(
                    tenant_id=tenant_id,
                    appointment_id=appointment_id,
                    action=action,
                    title=title,
                    old_start_at=old_start,
                    new_start_at=new_start,
                    actor=actor,
                    conversation_id=_as_uuid(conversation_id),
                )
                session.add(evento)
                await session.flush()
                if appointment_id is not None and action in ("programada", "reprogramada", "cancelada"):
                    # HU-SH-02: el equipo se entera de cada cita nueva, reagendada o cancelada.
                    from pack_calendar.demo import emitir_cambio

                    await emitir_cambio(
                        session,
                        appointment_id=appointment_id,
                        accion=action,
                        titulo=title,
                        inicio=new_start,
                        evento_id=evento.id,
                    )
        except Exception:
            from core.shared.logger import get_logger

            get_logger(__name__).warning("calendar_event_record_failed", exc_info=True)

    async def _client_appointments(
        self, arguments: dict[str, Any], context: ToolContext, *, upcoming_only: bool = False
    ) -> list[Any]:
        """Citas `scheduled` del cliente (tenant actual), ordenadas por fecha.

        SIEMPRE acotado al cliente o a la conversación del contexto (auditoría HU-SH-02):
        antes, un título o email del LLM podía traer citas de OTROS clientes del negocio
        y reagendarlas o cancelarlas. Dentro de ese conjunto, `event_id` (google o id
        local), `attendee_email` y título afinan. Con `upcoming_only`, solo las futuras.
        """
        from sqlalchemy import or_, select

        from core.db.session import get_session
        from pack_calendar.models import Appointment

        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return []
        event_id = str(arguments.get("event_id") or "").strip()
        conv_id = _as_uuid(context.conversation_id) if getattr(context, "conversation_id", None) else None
        cliente_id = _as_uuid(context.customer_id) if getattr(context, "customer_id", None) else None
        propias: list[Any] = []
        if cliente_id is not None:
            propias.append(Appointment.customer_id == cliente_id)
        if conv_id is not None:
            propias.append(Appointment.conversation_id == conv_id)
        if not propias:
            return []
        title = str(arguments.get("title") or "").strip()
        email = str(arguments.get("attendee_email") or "").strip()
        async with get_session() as session:
            q = select(Appointment).where(
                Appointment.tenant_id == tenant_id,
                Appointment.deleted_at.is_(None),
                Appointment.status == "scheduled",
                or_(*propias),
            )
            if upcoming_only:
                q = q.where(Appointment.start_at >= datetime.now(UTC))
            if event_id:
                cond = Appointment.google_event_id == event_id
                eid = _as_uuid(event_id)
                if eid is not None:
                    cond = or_(cond, Appointment.id == eid)
                q = q.where(cond)
            else:
                if email:
                    q = q.where(Appointment.attendee_email == email)
                if title:
                    q = q.where(Appointment.title.ilike(f"%{title}%"))
            rows = list((await session.execute(q.order_by(Appointment.start_at.asc()))).scalars().all())
        return rows

    async def _find_appointment(self, arguments: dict[str, Any], context: ToolContext) -> Any | None:
        """Localiza UNA cita para reagendar/cancelar: la más próxima futura (o la
        primera si no hay futuras) entre las coincidentes del cliente."""
        rows = await self._client_appointments(arguments, context)
        if not rows:
            return None
        now = datetime.now(UTC)
        future = [r for r in rows if r.start_at >= now]
        return future[0] if future else rows[0]

    async def list_appointments(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Enumera las citas agendadas (futuras) del cliente, numeradas y con id,
        con el inicio en la zona del negocio y la fecha escrita."""
        rows = await self._client_appointments(arguments, context, upcoming_only=True)
        tz = ZoneInfo(await self._timezone_name())
        return {
            "appointments": [
                {
                    "index": i + 1,
                    "event_id": r.google_event_id or str(r.id),
                    "title": r.title,
                    **_local_when(r.start_at, tz),
                }
                for i, r in enumerate(rows)
            ],
            "total": len(rows),
        }

    async def _send_client_notice(self, conversation_id: uuid.UUID | None, template: str, **values: str) -> bool:
        """Avisa al cliente por WhatsApp (best-effort) usando el template configurado."""
        if not conversation_id or not template:
            return False
        from core.db.models import Conversation
        from core.db.session import get_owned, get_session
        from core.gateway.sender import send_to_conversation

        tenant_id = get_current_tenant_id()
        async with get_session() as session:
            conv = await get_owned(session, Conversation, conversation_id, tenant_id)
        if conv is None or conv.bot_paused or conv.status == "closed":
            return False
        message = _render_template(template, **values)
        if not message:
            return False
        try:
            await send_to_conversation(conversation_id, message, sender_type="agent")
            return True
        except Exception:
            return False

    async def _notify_appointment_email(
        self,
        action: str,
        *,
        title: str,
        start: datetime | None,
        attendee_email: str | None,
        config: CalendarConfig,
        link: str = "",
    ) -> tuple[bool, str]:
        """Correo determinista de agendamiento (HU-CAL-02).

        Envía por el canal SMTP al cliente (`attendee_email`) y a los colaboradores
        con el permiso `pack_calendar:notificaciones`. Reemplaza los placeholders
        del asunto/cuerpo configurados. Devuelve `(enviado, motivo)`: si no hay
        conexión/email o falla, `enviado=False` y el motivo (el llamador lo registra
        y sigue; nunca rompe el flujo).

        `link` (HU-CAL-05): "agregar a mi calendario". Si la plantilla del negocio
        no trae `{link}`, se agrega una línea al final (salvo al cancelar).
        """
        from core.admin.permissions import notification_recipients
        from core.gateway.outbound_email import EmailOutboxError, queue_outbound_email

        templates = {
            "confirmed": (config.email_subject_confirmed, config.email_body_confirmed),
            "moved": (config.email_subject_moved, config.email_body_moved),
            "cancelled": (config.email_subject_cancelled, config.email_body_cancelled),
        }
        subject_t, body_t = templates.get(action, templates["confirmed"])
        tz = ZoneInfo(config.timezone)
        local = start.astimezone(tz) if start else None
        values = {
            "cliente": (attendee_email or "cliente"),
            "titulo": title or "tu cita",
            "fecha": local.strftime("%d/%m/%Y") if local else "",
            "hora": local.strftime("%H:%M") if local else "",
            "negocio": await self._business_name() or "el negocio",
            "link": link,
        }
        if link and action != "cancelled" and "{link}" not in body_t:
            body_t = body_t.rstrip() + "\n\nAgregá la cita a tu calendario: {link}"
        subject = _render_template(subject_t, **values)
        body = _render_template(body_t, **values)

        tenant_id = get_current_tenant_id()
        recipients: list[str] = []
        if attendee_email:
            recipients.append(attendee_email)
        try:
            for email in await notification_recipients("pack_calendar:notificaciones", tenant_id):
                if email not in recipients:
                    recipients.append(email)
        except Exception:
            pass

        if not recipients:
            return False, "sin_destinatarios"

        sent_any = False
        last_reason = ""
        for to in recipients:
            try:
                await queue_outbound_email(to, subject, body)
                sent_any = True
            except EmailOutboxError as exc:
                last_reason = exc.reason
            except Exception:
                last_reason = "error_envio"
        return sent_any, "" if sent_any else (last_reason or "error_envio")

    async def update_event(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Reagenda una cita existente (agenda interna o Google)."""
        from pack_calendar.config import DEFAULT_MOVED_MESSAGE

        source = await self._source()
        config = await self._tenant_config()
        tz = ZoneInfo(config.timezone)
        start = datetime.fromisoformat(str(arguments["start_datetime"]))
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)

        appt = await self._find_appointment(arguments, context)
        if appt is None and (arguments.get("event_id") or arguments.get("event_ids")):
            # El id pudo ser el NÚMERO de la lista o uno inventado/viejo: se
            # resuelve por referencia (nunca "la más próxima" a ciegas).
            refs = [
                str(arguments.get("event_id") or "").strip(),
                *[str(e).strip() for e in (arguments.get("event_ids") or [])],
            ]
            refs = [r for r in refs if r]
            resolved = await self._resolve_by_reference(arguments, context, refs)
            appt = resolved[0] if resolved else None
        if appt is None:
            return {
                "status": "not_found",
                "message": (
                    "No encontré una cita para reagendar con esos datos. Si el cliente tiene "
                    "varias citas, mostrale la lista numerada (list_client_appointments) y "
                    "pedile que elija; pasá el event_id REAL de la lista (no el número)."
                ),
            }

        duration = int(arguments.get("duration_minutes") or 0)
        if duration <= 0:
            duration = max(1, int((appt.end_at - appt.start_at).total_seconds() // 60))
        end = start + timedelta(minutes=duration)

        # Validación DURA al reagendar (HU-CAL-02): el nuevo horario no puede
        # chocar con otra cita/bloqueo. Se excluye la propia cita.
        if source != "google":
            conflict = await self._validate_internal_slot(
                config, start, end, exclude_appt_id=appt.id
            )
            if conflict is not None:
                return {
                    "status": "rejected",
                    "reason": conflict,
                    "message": {
                        "occupied": "Ese horario ya está ocupado. Ofrecé otro de free_slots.",
                        "outside_hours": "Ese horario está fuera del horario de atención.",
                        "in_past": "Esa fecha/hora ya pasó.",
                        "invalid_range": "El fin debe ser posterior al inicio.",
                    }.get(conflict, "Horario inválido."),
                }

        calendar_link = _calendar_link(
            start, end, appt.title or "Cita", f"Cita agendada con {await self._business_name()}", config.timezone
        )
        link = calendar_link
        if source == "google" and appt.google_event_id:
            token = await self._google_token(config)
            body: dict[str, Any] = {
                "start": {"dateTime": start.isoformat(), "timeZone": config.timezone},
                "end": {"dateTime": end.isoformat(), "timeZone": config.timezone},
            }
            event = await _google_patch_event(config, token, appt.google_event_id, body)
            link = event.get("htmlLink", "") or calendar_link

        from core.db.models import utcnow
        from core.db.session import get_owned, get_session
        from pack_calendar.models import Appointment

        old_start = appt.start_at
        from pack_calendar.demo import es_solape

        try:
            async with get_session() as session:
                row = await get_owned(session, Appointment, appt.id, get_current_tenant_id())
                if row is not None:
                    row.start_at = start
                    row.end_at = end
                    row.updated_at = utcnow()
        except Exception as exc:
            if not es_solape(exc):
                raise
            return {
                "status": "rejected",
                "reason": "occupied",
                "message": "Ese horario acaba de ocuparse. Ofrecé otro de check_availability.",
            }

        await self._record_event(
            action="reprogramada",
            title=appt.title or "",
            appointment_id=appt.id,
            conversation_id=appt.conversation_id,
            old_start=old_start,
            new_start=start,
        )

        fecha = start.astimezone(tz).strftime("%d/%m/%Y")
        hora = start.astimezone(tz).strftime("%H:%M")
        notified = await self._send_client_notice(
            appt.conversation_id,
            getattr(config, "appointment_moved_message", "") or DEFAULT_MOVED_MESSAGE,
            fecha=fecha,
            hora=hora,
            titulo=appt.title or "tu cita",
            link=calendar_link,
        )
        email_sent, email_reason = await self._notify_appointment_email(
            "moved",
            title=appt.title or "tu cita",
            start=start,
            attendee_email=appt.attendee_email,
            config=config,
            link=calendar_link,
        )
        return {
            "status": "updated",
            "event_id": appt.google_event_id or str(appt.id),
            **_local_when(start, tz),
            "notified": notified,
            "email_sent": email_sent,
            "email_reason": email_reason,
            "_safe_append": (
                f"📅 Tu cita quedó reagendada para el {fecha} a las {hora}: {link}"
                if link
                else f"📅 Tu cita quedó reagendada para el {fecha} a las {hora}."
            ),
        }

    async def _resolve_by_reference(
        self, arguments: dict[str, Any], context: ToolContext, references: list[str]
    ) -> list[Any]:
        """Resuelve referencias ambiguas a citas (corrección 2026-10-05: el modelo
        pasó `event_id: "2"` —el número de la lista— y el fallback canceló LA CITA
        EQUIVOCADA, la más próxima).

        Reglas:
        - Referencia numérica ("2"): la N-ésima cita FUTURA ordenada por fecha,
          el mismo orden que `list_client_appointments` mostró al cliente.
        - Referencia no reconocida: solo si queda **exactamente una** cita tras
          aplicar los filtros (conversación/título/email). Con varias NO se elige
          a ciegas: devuelve [] y el llamador pide aclaración.
        """
        fallback = {k: v for k, v in arguments.items() if k not in ("event_id", "event_ids")}
        rows = await self._client_appointments(fallback, context, upcoming_only=True)
        if len(references) == 1 and references[0].strip().isdigit():
            index = int(references[0].strip())
            if 1 <= index <= len(rows):
                return [rows[index - 1]]
            return []
        if len(rows) == 1:
            return rows
        return []

    async def cancel_event(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Cancela UNA o VARIAS citas (o todas) y notifica por WhatsApp."""
        from pack_calendar.config import DEFAULT_CANCELLED_MESSAGE, DEFAULT_CANCELLED_SUMMARY

        source = await self._source()
        config = await self._tenant_config()
        event_ids = [str(e).strip() for e in (arguments.get("event_ids") or []) if str(e).strip()]
        cancel_all = bool(arguments.get("cancel_all", False))
        single_id = str(arguments.get("event_id") or "").strip()

        rows: list[Any] = []
        if cancel_all:
            rows = await self._client_appointments(arguments, context, upcoming_only=True)
        elif event_ids or single_id:
            client_rows = await self._client_appointments(arguments, context, upcoming_only=True)
            wanted = set(event_ids) | ({single_id} if single_id else set())
            rows = [r for r in client_rows if (r.google_event_id in wanted) or (str(r.id) in wanted)]
            if not rows:
                # El id pudo ser el NÚMERO de la lista o uno inventado/viejo: se
                # resuelve por referencia (nunca "la más próxima" a ciegas).
                refs = event_ids or ([single_id] if single_id else [])
                rows = await self._resolve_by_reference(arguments, context, refs)
        else:
            appt = await self._find_appointment(arguments, context)
            rows = [appt] if appt else []

        if not rows:
            return {
                "status": "not_found",
                "message": (
                    "No encontré una cita para cancelar con esos datos. Si el cliente tiene "
                    "varias citas, mostrale la lista numerada (list_client_appointments) y "
                    "pedile que elija; pasá el event_id REAL de la lista (no el número)."
                ),
            }

        from core.db.models import utcnow
        from core.db.session import get_owned, get_session
        from pack_calendar.models import Appointment

        cancelled_ids: list[str] = []
        for row in rows:
            if source == "google" and row.google_event_id:
                try:
                    token = await self._google_token(config)
                    await _google_delete_event(config, token, row.google_event_id)
                except Exception:
                    pass  # best-effort: el espejo local manda para el panel

            async with get_session() as session:
                db_row = await get_owned(session, Appointment, row.id, get_current_tenant_id())
                if db_row is not None:
                    db_row.status = "cancelled"
                    db_row.updated_at = utcnow()

            await self._record_event(
                action="cancelada",
                title=row.title or "",
                appointment_id=row.id,
                conversation_id=row.conversation_id,
                old_start=row.start_at,
            )
            cancelled_ids.append(row.google_event_id or str(row.id))

        if len(rows) == 1:
            tz = ZoneInfo(config.timezone)
            fecha = rows[0].start_at.astimezone(tz).strftime("%d/%m/%Y") if rows[0].start_at else ""
            hora = rows[0].start_at.astimezone(tz).strftime("%H:%M") if rows[0].start_at else ""
            notified = await self._send_client_notice(
                rows[0].conversation_id,
                getattr(config, "appointment_cancelled_message", "") or DEFAULT_CANCELLED_MESSAGE,
                fecha=fecha,
                hora=hora,
                titulo=rows[0].title or "tu cita",
            )
            email_sent, email_reason = await self._notify_appointment_email(
                "cancelled",
                title=rows[0].title or "tu cita",
                start=rows[0].start_at,
                attendee_email=rows[0].attendee_email,
                config=config,
            )
        else:
            notified = await self._send_client_notice(
                rows[0].conversation_id,
                getattr(config, "appointments_cancelled_summary", "") or DEFAULT_CANCELLED_SUMMARY,
                cantidad=str(len(rows)),
            )
            email_sent, email_reason = await self._notify_appointment_email(
                "cancelled",
                title=rows[0].title or "tu cita",
                start=rows[0].start_at,
                attendee_email=rows[0].attendee_email,
                config=config,
            )

        return {
            "status": "cancelled",
            "cancelled": cancelled_ids,
            "notified": notified,
            "email_sent": email_sent,
            "email_reason": email_reason,
            "event_id": cancelled_ids[0] if cancelled_ids else None,
        }

    async def _local_busy(
        self, date: str, tz: ZoneInfo, exclude_appt_id: uuid.UUID | None = None
    ) -> tuple[list[dict[str, str]], list[tuple[datetime, datetime]]]:
        """Ocupados de la agenda interna del día: citas agendadas + bloqueos.

        `exclude_appt_id` omite una cita (al reagendar, no debe chocar consigo misma).
        """
        from sqlalchemy import select

        from core.db.session import get_session
        from pack_calendar.models import Appointment, CalendarBlock

        tenant_id = get_current_tenant_id()
        day_start = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        out: list[dict[str, str]] = []
        intervals: list[tuple[datetime, datetime]] = []
        async with get_session() as session:
            appts = (
                await session.execute(
                    select(Appointment).where(
                        Appointment.tenant_id == tenant_id,
                        Appointment.deleted_at.is_(None),
                        Appointment.status == "scheduled",
                        Appointment.start_at < day_end,
                        Appointment.end_at > day_start,
                    )
                )
            ).scalars().all()
            blocks = (
                await session.execute(
                    select(CalendarBlock).where(
                        CalendarBlock.tenant_id == tenant_id,
                        CalendarBlock.deleted_at.is_(None),
                        CalendarBlock.start_at < day_end,
                        CalendarBlock.end_at > day_start,
                    )
                )
            ).scalars().all()
        for a in appts:
            if exclude_appt_id is not None and a.id == exclude_appt_id:
                continue
            out.append(
                {
                    "start": a.start_at.astimezone(tz).isoformat(),
                    "end": a.end_at.astimezone(tz).isoformat(),
                    # Sin el título: puede llevar el nombre de OTRO cliente (auditoría HU-SH-02).
                    "summary": "(ocupado)",
                }
            )
            intervals.append((a.start_at, a.end_at))
        for b in blocks:
            out.append(
                {
                    "start": b.start_at.astimezone(tz).isoformat(),
                    "end": b.end_at.astimezone(tz).isoformat(),
                    "summary": b.reason or "(bloqueado)",
                }
            )
            intervals.append((b.start_at, b.end_at))
        return out, intervals

    async def _validate_internal_slot(
        self,
        config: CalendarConfig,
        start: datetime,
        end: datetime,
        *,
        exclude_appt_id: uuid.UUID | None = None,
    ) -> str | None:
        """Validación DURA del horario en modo interno (HU-CAL-02).

        Recalcula los ocupados reales del día y rechaza solapamiento, fuera de
        horario o fecha pasada. Devuelve el motivo o None. No aplica a `google`
        (allí manda la disponibilidad de Google Calendar).
        """
        tz = ZoneInfo(config.timezone)
        if end <= start:
            return "invalid_range"
        date = start.astimezone(tz).strftime("%Y-%m-%d")
        _busy, intervals = await self._local_busy(date, tz, exclude_appt_id=exclude_appt_id)
        return _slot_conflict(
            _open_window(config, date),
            intervals,
            tz,
            start,
            end,
            buffer_minutes=config.buffer_minutes,
        )

    async def _busy_for_day(
        self, config: CalendarConfig, source: str, day: str, tz: ZoneInfo
    ) -> tuple[list[dict[str, str]], list[tuple[datetime, datetime]]]:
        """Ocupados del día (Google o agenda interna) como lista legible + intervalos."""
        if source != "google":
            return await self._local_busy(day, tz)
        token = await self._google_token(config)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{CALENDAR_API}/calendars/{config.calendar_id}/events",
                params={
                    "timeMin": f"{day}T00:00:00Z",
                    "timeMax": f"{day}T23:59:59Z",
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise PluginError(PLUGIN_ID, f"Calendar API {response.status_code}: {response.text[:200]}")
        events = response.json().get("items", [])
        busy = [
            {
                "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date")),
                "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date")),
                "summary": e.get("summary", "(ocupado)"),
            }
            for e in events
        ]
        intervals = [iv for iv in (_google_interval(e, tz) for e in events) if iv is not None]
        return busy, intervals

    async def check_availability(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        source = await self._source()
        config = await self._tenant_config()
        tz = ZoneInfo(config.timezone)
        day_s = str(arguments.get("date") or "").strip()
        try:
            day = date.fromisoformat(day_s)
        except ValueError:
            return {"error": "Fecha inválida: usá el formato YYYY-MM-DD.", "status": "invalid_date"}
        duration = int(arguments.get("duration_minutes") or 0) or config.event_duration_minutes

        # Nunca fechas pasadas (HU-CAL-05): el modelo llegó a consultar meses atrás.
        now = datetime.now(tz)
        today = now.date()
        if day < today:
            return {
                "status": "in_past",
                "today": today.isoformat(),
                "message": f"Esa fecha ya pasó; hoy es {_long_date(today)}.",
            }

        busy, intervals = await self._busy_for_day(config, source, day_s, tz)
        window = _open_window(config, day_s)
        return {
            "status": "ok",
            "today": today.isoformat(),
            "date": day_s,
            "source": source,
            "business_hours": window,
            "slot_minutes": config.slot_minutes,
            "buffer_minutes": config.buffer_minutes,
            "duration_minutes": duration,
            "busy_slots": busy,
            "free_slots": _free_slots(
                window,
                intervals,
                tz,
                day_s,
                slot_minutes=config.slot_minutes,
                duration_minutes=duration,
                buffer_minutes=config.buffer_minutes,
                not_before=now if day == today else None,
            ),
            "total_events": len(busy),
        }

    async def find_next_available(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Próximos días (desde hoy) con horarios libres, para cuando el cliente no
        da fecha o pide "lo más pronto" (HU-CAL-05). Salta días cerrados y días sin
        cupo; devuelve como máximo `_NEXT_AVAILABLE_MAX_RESULTS` días."""
        try:
            days = int(arguments.get("days") or 7)
            duration = int(arguments.get("duration_minutes") or 0)
        except (TypeError, ValueError):
            days, duration = 0, 0
        if not 1 <= days <= _NEXT_AVAILABLE_MAX_DAYS:
            return {
                "error": f"days debe estar entre 1 y {_NEXT_AVAILABLE_MAX_DAYS}.",
                "status": "invalid_range",
            }
        source = await self._source()
        config = await self._tenant_config()
        tz = ZoneInfo(config.timezone)
        duration = duration if duration > 0 else config.event_duration_minutes
        now = datetime.now(tz)
        today = now.date()

        found: list[dict[str, Any]] = []
        for offset in range(days):
            day = today + timedelta(days=offset)
            day_s = day.isoformat()
            window = _open_window(config, day_s)
            if not window:
                continue
            _busy, intervals = await self._busy_for_day(config, source, day_s, tz)
            free = _free_slots(
                window,
                intervals,
                tz,
                day_s,
                slot_minutes=config.slot_minutes,
                duration_minutes=duration,
                buffer_minutes=config.buffer_minutes,
                not_before=now if day == today else None,
            )
            if not free:
                continue
            found.append({"date": day_s, "weekday": _WEEKDAY_NAMES[day.weekday()], "free_slots": free})
            if len(found) >= _NEXT_AVAILABLE_MAX_RESULTS:
                break

        if not found:
            return {
                "status": "no_availability",
                "today": today.isoformat(),
                "message": f"No hay horarios libres en los próximos {days} días.",
            }
        return {
            "status": "ok",
            "today": today.isoformat(),
            "duration_minutes": duration,
            "days": found,
            "total_days": len(found),
        }

def _google_interval(event: dict[str, Any], tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """Intervalo (start, end) tz-aware de un evento de Google; None si no parsea."""
    start = event.get("start", {}) or {}
    end = event.get("end", {}) or {}
    try:
        if "dateTime" in start:
            s = datetime.fromisoformat(start["dateTime"])
            e = datetime.fromisoformat(end.get("dateTime", start["dateTime"]))
            if s.tzinfo is None:
                s = s.replace(tzinfo=tz)
            if e.tzinfo is None:
                e = e.replace(tzinfo=tz)
            return s, e
        if "date" in start:
            s = datetime.fromisoformat(start["date"]).replace(tzinfo=tz)
            e = datetime.fromisoformat(end.get("date", start["date"])).replace(tzinfo=tz)
            return s, e
    except (ValueError, TypeError):
        return None
    return None

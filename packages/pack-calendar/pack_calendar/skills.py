"""CalendarSkill: agendamiento de citas contra Google Calendar API.

La cita se espeja en `pack_calendar_appointments` (para el panel) con su
`google_event_id`. `check_availability` considera el horario de atención
configurado (`CalendarConfig.business_hours`) además de los eventos ocupados.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
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


class CalendarSkill(BaseSkill):
    name = "calendar_skill"
    description = "Agendamiento de citas en Google Calendar"
    keywords = ["cita", "agendar", "reservar", "reunión", "disponibilidad", "horario", "calendario"]

    async def _tenant_config(self) -> CalendarConfig:
        """Config del pack para el tenant ACTUAL (no una capturada en activación).

        Sin esto, el skill quedaría con el calendario de un solo tenant mientras
        los tokens se resuelven por tenant: una fuga cruzada (escribir citas del
        tenant B en el calendario del A). Carga por invocación.
        """
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
        if config is None:
            raise PluginError(PLUGIN_ID, "Google Calendar no configurado para este tenant")
        return config

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="create_calendar_event",
                description=(
                    "Crea una cita en el calendario del negocio. Úsala SOLO cuando el cliente "
                    "haya confirmado fecha y hora exactas. Fechas en formato ISO: 2026-08-20T15:00:00"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Título, ej: 'Cita con Carlos Ruiz'"},
                        "start_datetime": {"type": "string", "description": "Inicio ISO 8601 (hora local del negocio)"},
                        "description": {"type": "string", "description": "Detalle de la cita"},
                        "attendee_email": {"type": "string", "description": "Email del cliente si lo dio"},
                    },
                    "required": ["title", "start_datetime"],
                },
                handler=self.create_event,
                requires_connection_types=["google_calendar"],
            ),
            MCPTool(
                name="check_availability",
                description=(
                    "Consulta los eventos ya agendados y el horario de atención de una fecha "
                    "para proponer horarios libres. Úsala ANTES de crear una cita."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Fecha a consultar, formato YYYY-MM-DD"},
                    },
                    "required": ["date"],
                },
                handler=self.check_availability,
                requires_connection_types=["google_calendar"],
            ),
            MCPTool(
                name="update_calendar_event",
                description=(
                    "Reagenda/reprograma una cita YA EXISTENTE a una nueva fecha/hora (la mueve, "
                    "no crea otra). Úsala SOLO cuando el cliente haya confirmado la nueva fecha y "
                    "hora exactas. Si conocés el event_id que devolvió create_calendar_event, "
                    "pasalo; si no, el sistema busca la próxima cita agendada del cliente por "
                    "título o email. Fechas ISO 8601 en hora local del negocio."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "start_datetime": {
                            "type": "string",
                            "description": "Nueva fecha/hora ISO 8601 (hora local del negocio), ej: 2026-09-30T14:00:00",
                        },
                        "event_id": {
                            "type": "string",
                            "description": "ID del evento de Google (lo devuelve create_calendar_event). Opcional.",
                        },
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
                requires_connection_types=["google_calendar"],
            ),
            MCPTool(
                name="cancel_calendar_event",
                description=(
                    "Cancela una cita YA EXISTENTE (borra el evento de Google y la marca cancelada). "
                    "Úsala SOLO cuando el cliente pidió cancelar explícitamente su cita/demo. Si "
                    "conocés el event_id, pasalo; si no, el sistema busca la próxima cita agendada "
                    "del cliente por título o email."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "ID del evento de Google (opcional)"},
                        "title": {"type": "string", "description": "Título de la cita para localizarla (opcional)"},
                        "attendee_email": {"type": "string", "description": "Email del cliente para localizarla (opcional)"},
                    },
                    "required": [],
                },
                handler=self.cancel_event,
                requires_connection_types=["google_calendar"],
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def create_event(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        config = await self._tenant_config()
        token = await get_access_token(
            config.client_id, config.client_secret.get_secret_value()
        )
        tz = ZoneInfo(config.timezone)
        start = datetime.fromisoformat(str(arguments["start_datetime"]))
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        end = start + timedelta(minutes=config.event_duration_minutes)

        body: dict[str, Any] = {
            "summary": str(arguments.get("title", "Cita")),
            "description": str(arguments.get("description", ""))
            + f"\n[agent-core conversación {context.conversation_id}]",
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

        await self._mirror_appointment(event, arguments, context, start, end)

        return {
            "event_id": event.get("id"),
            "status": "created",
            "start": start.isoformat(),
            "link": event.get("htmlLink", ""),
            # SafeRenderer: el link exacto lo adjunta el core, el LLM no lo copia a mano
            "_safe_append": f"📅 Tu cita quedó agendada: {event.get('htmlLink', '')}",
        }

    async def _mirror_appointment(
        self,
        event: dict[str, Any],
        arguments: dict[str, Any],
        context: ToolContext,
        start: datetime,
        end: datetime,
    ) -> None:
        """Espeja la cita en la tabla del pack (para el panel). Best-effort."""
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            from core.shared.logger import get_logger

            get_logger(__name__).warning("calendar_mirror_sin_tenant")
            return
        try:
            from core.db.session import get_session
            from pack_calendar.models import Appointment

            async with get_session() as session:
                session.add(
                    Appointment(
                        tenant_id=tenant_id,
                        conversation_id=_as_uuid(context.conversation_id),
                        customer_id=_as_uuid(context.customer_id),
                        google_event_id=event.get("id"),
                        title=str(arguments.get("title", "Cita")),
                        attendee_email=(
                            str(arguments["attendee_email"])
                            if arguments.get("attendee_email")
                            else None
                        ),
                        start_at=start,
                        end_at=end,
                        status="scheduled",
                    )
                )
        except Exception:  # nunca tumbar el agendamiento real por el espejo
            from core.shared.logger import get_logger

            get_logger(__name__).warning("calendar_mirror_failed", exc_info=True)

    async def _find_appointment(self, arguments: dict[str, Any], context: ToolContext) -> Any | None:
        """Localiza la cita a reagendar/cancelar.

        Si se pasa `event_id` (google_event_id), coincidencia exacta. Si no, se
        filtran las citas agendadas del tenant por conversación/título/email y se
        elige la más próxima futura (o la primera si no hay futuras).
        """
        from sqlalchemy import or_, select

        from core.db.session import get_session
        from pack_calendar.models import Appointment

        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return None
        event_id = str(arguments.get("event_id") or "").strip()
        async with get_session() as session:
            q = select(Appointment).where(
                Appointment.tenant_id == tenant_id,
                Appointment.deleted_at.is_(None),
                Appointment.status == "scheduled",
            )
            if event_id:
                q = q.where(Appointment.google_event_id == event_id)
            else:
                conv_id = (
                    _as_uuid(context.conversation_id) if getattr(context, "conversation_id", None) else None
                )
                title = str(arguments.get("title") or "").strip()
                email = str(arguments.get("attendee_email") or "").strip()
                filters: list[Any] = []
                if conv_id is not None:
                    filters.append(Appointment.conversation_id == conv_id)
                if title:
                    filters.append(Appointment.title.ilike(f"%{title}%"))
                if email:
                    filters.append(Appointment.attendee_email == email)
                if not filters:
                    return None
                q = q.where(or_(*filters))
            rows = list(
                (await session.execute(q.order_by(Appointment.start_at.asc()).limit(20))).scalars().all()
            )
        if not rows:
            return None
        now = datetime.now(UTC)
        future = [r for r in rows if r.start_at >= now]
        return future[0] if future else rows[0]

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
        message = template.format(**values)
        if not message:
            return False
        try:
            await send_to_conversation(conversation_id, message, sender_type="agent")
            return True
        except Exception:
            return False

    async def update_event(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Reagenda una cita existente: mueve el evento en Google y el espejo local."""
        from pack_calendar.config import DEFAULT_MOVED_MESSAGE

        config = await self._tenant_config()
        token = await get_access_token(
            config.client_id, config.client_secret.get_secret_value()
        )
        tz = ZoneInfo(config.timezone)
        start = datetime.fromisoformat(str(arguments["start_datetime"]))
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)

        appt = await self._find_appointment(arguments, context)
        if appt is None:
            return {"status": "not_found", "message": "No encontré una cita agendada para reagendar."}

        duration = int(arguments.get("duration_minutes") or 0)
        if duration <= 0:
            duration = max(1, int((appt.end_at - appt.start_at).total_seconds() // 60))
        end = start + timedelta(minutes=duration)

        body: dict[str, Any] = {
            "start": {"dateTime": start.isoformat(), "timeZone": config.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": config.timezone},
        }
        event: dict[str, Any] = {}
        if appt.google_event_id:
            event = await _google_patch_event(config, token, appt.google_event_id, body)

        from core.db.models import utcnow
        from core.db.session import get_owned, get_session
        from pack_calendar.models import Appointment

        async with get_session() as session:
            row = await get_owned(session, Appointment, appt.id, get_current_tenant_id())
            if row is not None:
                row.start_at = start
                row.end_at = end
                row.updated_at = utcnow()

        fecha = start.astimezone(tz).strftime("%d/%m/%Y")
        hora = start.astimezone(tz).strftime("%H:%M")
        notified = await self._send_client_notice(
            appt.conversation_id,
            getattr(config, "appointment_moved_message", "") or DEFAULT_MOVED_MESSAGE,
            fecha=fecha,
            hora=hora,
            titulo=appt.title or "tu cita",
        )
        return {
            "status": "updated",
            "event_id": appt.google_event_id,
            "start": start.isoformat(),
            "notified": notified,
            "link": event.get("htmlLink", ""),
            "_safe_append": f"📅 Tu cita quedó reagendada para el {fecha} a las {hora}: {event.get('htmlLink', '')}",
        }

    async def cancel_event(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Cancela una cita existente: borra el evento de Google y marca el espejo local."""
        from pack_calendar.config import DEFAULT_CANCELLED_MESSAGE

        config = await self._tenant_config()
        appt = await self._find_appointment(arguments, context)
        if appt is None:
            return {"status": "not_found", "message": "No encontré una cita agendada para cancelar."}

        if appt.google_event_id:
            try:
                token = await get_access_token(
                    config.client_id, config.client_secret.get_secret_value()
                )
                await _google_delete_event(config, token, appt.google_event_id)
            except Exception:
                pass  # best-effort: el espejo local manda para el panel

        from core.db.models import utcnow
        from core.db.session import get_owned, get_session
        from pack_calendar.models import Appointment

        async with get_session() as session:
            row = await get_owned(session, Appointment, appt.id, get_current_tenant_id())
            if row is not None:
                row.status = "cancelled"
                row.updated_at = utcnow()

        tz = ZoneInfo(config.timezone)
        fecha = appt.start_at.astimezone(tz).strftime("%d/%m/%Y") if appt.start_at else ""
        hora = appt.start_at.astimezone(tz).strftime("%H:%M") if appt.start_at else ""
        notified = await self._send_client_notice(
            appt.conversation_id,
            getattr(config, "appointment_cancelled_message", "") or DEFAULT_CANCELLED_MESSAGE,
            fecha=fecha,
            hora=hora,
            titulo=appt.title or "tu cita",
        )
        return {"status": "cancelled", "event_id": appt.google_event_id, "notified": notified}

    async def check_availability(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        config = await self._tenant_config()
        token = await get_access_token(
            config.client_id, config.client_secret.get_secret_value()
        )
        date = str(arguments["date"])
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{CALENDAR_API}/calendars/{config.calendar_id}/events",
                params={
                    "timeMin": f"{date}T00:00:00Z",
                    "timeMax": f"{date}T23:59:59Z",
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
        return {
            "date": date,
            "business_hours": _open_window(config, date),
            "slot_minutes": config.slot_minutes,
            "buffer_minutes": config.buffer_minutes,
            "busy_slots": busy,
            "total_events": len(busy),
        }


def _open_window(config: CalendarConfig, date: str) -> str:
    """Ventana de atención ("HH:MM-HH:MM") del día, o "" si está cerrado."""
    try:
        parsed = json.loads(config.business_hours or "{}")
        weekday = datetime.fromisoformat(date).weekday()
        return str(parsed.get(WEEKDAYS[weekday], "") or "")
    except (ValueError, json.JSONDecodeError):
        return ""

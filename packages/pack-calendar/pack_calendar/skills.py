"""CalendarSkill: agendamiento de citas contra Google Calendar API.

La cita se espeja en `pack_calendar_appointments` (para el panel) con su
`google_event_id`. `check_availability` considera el horario de atención
configurado (`CalendarConfig.business_hours`) además de los eventos ocupados.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
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

"""CalendarSkill: agendamiento de citas contra Google Calendar API."""

from __future__ import annotations

from typing import Any

import httpx

from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.exceptions import PluginError
from mod_google_calendar.config import CalendarConfig
from mod_google_calendar.oauth import get_access_token

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
PLUGIN_ID = "mod_google_calendar"


class CalendarSkill(BaseSkill):
    name = "calendar_skill"
    description = "Agendamiento de citas en Google Calendar"
    keywords = ["cita", "agendar", "reservar", "reunión", "disponibilidad", "horario", "calendario"]

    def __init__(self, config: CalendarConfig) -> None:
        self._config = config

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
                    "Consulta los eventos ya agendados en una fecha para proponer horarios libres. "
                    "Úsala ANTES de crear una cita."
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
        from datetime import datetime, timedelta

        token = await get_access_token(
            self._config.client_id, self._config.client_secret.get_secret_value()
        )
        start = datetime.fromisoformat(str(arguments["start_datetime"]))
        end = start + timedelta(minutes=self._config.event_duration_minutes)

        body: dict[str, Any] = {
            "summary": str(arguments.get("title", "Cita")),
            "description": str(arguments.get("description", ""))
            + f"\n[agent-core conversación {context.conversation_id}]",
            "start": {"dateTime": start.isoformat(), "timeZone": self._config.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self._config.timezone},
        }
        if arguments.get("attendee_email"):
            body["attendees"] = [{"email": str(arguments["attendee_email"])}]

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{CALENDAR_API}/calendars/{self._config.calendar_id}/events",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise PluginError(PLUGIN_ID, f"Calendar API {response.status_code}: {response.text[:200]}")
        event = response.json()
        return {
            "event_id": event.get("id"),
            "status": "created",
            "start": start.isoformat(),
            "link": event.get("htmlLink", ""),
            # SafeRenderer: el link exacto lo adjunta el core, el LLM no lo copia a mano
            "_safe_append": f"📅 Tu cita quedó agendada: {event.get('htmlLink', '')}",
        }

    async def check_availability(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        token = await get_access_token(
            self._config.client_id, self._config.client_secret.get_secret_value()
        )
        date = str(arguments["date"])
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{CALENDAR_API}/calendars/{self._config.calendar_id}/events",
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
        return {"date": date, "busy_slots": busy, "total_events": len(busy)}

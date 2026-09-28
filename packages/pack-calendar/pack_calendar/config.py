"""Configuración de Google Calendar.

El formulario del panel pide client_id y client_secret (OAuth2 de Google Cloud).
Los tokens de acceso/refresh NO van aquí: viven en la conexión del tenant
(`channel_connections`, tipo `google_calendar`, ADR-0070).
"""

from __future__ import annotations

from pydantic import Field, SecretStr

from core.mcp.types import BasePluginConfig

# Lunes a domingo; "" = cerrado. Formato "HH:MM-HH:MM" (hora local del negocio).
DEFAULT_BUSINESS_HOURS = (
    '{"mon": "09:00-19:00", "tue": "09:00-19:00", "wed": "09:00-19:00", '
    '"thu": "09:00-19:00", "fri": "09:00-19:00", "sat": "09:00-13:00", "sun": ""}'
)

# Mensaje por defecto al cliente cuando se mueve su cita (WhatsApp).
DEFAULT_MOVED_MESSAGE = (
    "Tu cita fue reprogramada para el {fecha} a las {hora}. Te esperamos. 💬"
)

# Mensaje por defecto al cliente cuando se cancela su cita (WhatsApp).
DEFAULT_CANCELLED_MESSAGE = (
    "Tu cita quedó cancelada. Si querés reagendarla, avisanos y lo ajustamos. 💬"
)


class CalendarConfig(BasePluginConfig):
    client_id: str = Field(..., description="Client ID de OAuth2 (Google Cloud Console)")
    client_secret: SecretStr = Field(..., description="Client Secret de OAuth2")
    calendar_id: str = Field(
        default="primary", description="ID del calendario (primary = calendario principal)"
    )
    timezone: str = Field(default="America/Bogota", description="Zona horaria de las citas")
    event_duration_minutes: int = Field(default=60, description="Duración por defecto de una cita")
    slot_minutes: int = Field(default=60, description="Tamaño del bloque al proponer horarios")
    buffer_minutes: int = Field(default=0, description="Margen entre citas")
    business_hours: str = Field(
        default=DEFAULT_BUSINESS_HOURS,
        description=(
            "Horario de atención por día (JSON: día -> 'HH:MM-HH:MM'; \"\" = cerrado). "
            "Días: mon, tue, wed, thu, fri, sat, sun"
        ),
        json_schema_extra={"format": "textarea"},
    )
    appointment_moved_message: str = Field(
        default=DEFAULT_MOVED_MESSAGE,
        description=(
            "Mensaje que recibe el cliente por WhatsApp cuando se mueve su cita. "
            "Placeholders: {fecha}, {hora}, {titulo}"
        ),
        json_schema_extra={"format": "textarea"},
    )
    appointment_cancelled_message: str = Field(
        default=DEFAULT_CANCELLED_MESSAGE,
        description=(
            "Mensaje que recibe el cliente por WhatsApp cuando se cancela su cita. "
            "Placeholders: {fecha}, {hora}, {titulo}"
        ),
        json_schema_extra={"format": "textarea"},
    )

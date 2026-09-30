"""Configuración de Google Calendar.

El formulario del panel pide client_id y client_secret (OAuth2 de Google Cloud).
Los tokens de acceso/refresh NO van aquí: viven en la conexión del tenant
(`channel_connections`, tipo `google_calendar`, ADR-0070).
"""

from __future__ import annotations

from pydantic import Field, SecretStr, model_validator

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

# Mensaje resumen por defecto al cancelar varias citas (WhatsApp).
DEFAULT_CANCELLED_SUMMARY = (
    "Se cancelaron {cantidad} citas. Si querés reagendarlas, avisanos. 💬"
)


class CalendarConfig(BasePluginConfig):
    source: str = Field(
        default="interno",
        description=(
            "Fuente de la agenda: 'interno' (agenda propia en la plataforma, sin Google) "
            "o 'google' (Google Calendar, requiere OAuth)"
        ),
    )
    client_id: str = Field(
        default="", description="Client ID de OAuth2 (Google Cloud Console) — solo modo 'google'"
    )
    client_secret: SecretStr = Field(
        default=SecretStr(""), description="Client Secret de OAuth2 — solo modo 'google'"
    )
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
    appointments_cancelled_summary: str = Field(
        default=DEFAULT_CANCELLED_SUMMARY,
        description=(
            "Mensaje resumen que recibe el cliente por WhatsApp cuando se cancelan "
            "VARIAS citas a la vez. Placeholder: {cantidad}"
        ),
        json_schema_extra={"format": "textarea"},
    )

    @model_validator(mode="after")
    def _validate_source(self) -> CalendarConfig:
        """Modo `google` exige credenciales OAuth; `interno` no las necesita."""
        if self.source not in ("interno", "google"):
            raise ValueError("source debe ser 'interno' o 'google'")
        if self.source == "google" and (
            not self.client_id or not self.client_secret.get_secret_value()
        ):
            raise ValueError("El modo 'google' requiere client_id y client_secret")
        return self

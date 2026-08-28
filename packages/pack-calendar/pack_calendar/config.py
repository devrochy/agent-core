"""Configuración de Google Calendar.

El formulario del panel pide client_id y client_secret (OAuth2 de Google Cloud).
Los tokens de acceso/refresh NO van aquí: se obtienen con el flujo OAuth
(/admin/plugins/pack_calendar/oauth) y se guardan cifrados aparte.
"""

from __future__ import annotations

from pydantic import Field, SecretStr

from core.mcp.types import BasePluginConfig


class CalendarConfig(BasePluginConfig):
    client_id: str = Field(..., description="Client ID de OAuth2 (Google Cloud Console)")
    client_secret: SecretStr = Field(..., description="Client Secret de OAuth2")
    calendar_id: str = Field(
        default="primary", description="ID del calendario (primary = calendario principal)"
    )
    timezone: str = Field(default="America/Bogota", description="Zona horaria de las citas")
    event_duration_minutes: int = Field(default=60, description="Duración por defecto de una cita")

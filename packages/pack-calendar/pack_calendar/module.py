"""GoogleCalendarModule: pack-calendar v2 (ADR-0069/0070).

- `admin_router` = panel propio `/calendar/*` con navegación por secciones
  (ADR-0061/0063): Conexión · Horarios y calendario · Tipos de cita · Citas.
- El router OAuth conserva su URL `/admin/plugins/pack_calendar/oauth[/callback]`
  (redirect URI registrado en Google Console, ADR-0070), por eso va como
  `extra_admin_routers`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from core.mcp.types import BaseModule, GuardrailResult
from core.shared.logger import get_logger
from pack_calendar.admin import router as calendar_admin_router
from pack_calendar.admin_api import router as calendar_admin_api_router
from pack_calendar.config import CalendarConfig
from pack_calendar.skills import CalendarSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

PLUGIN_ID = "pack_calendar"

log = get_logger(__name__)

# OAuth: conserva la URL vieja (ver docstring).


# Afirmaciones del LLM sobre acciones de citas que SOLO son válidas si la tool
# correspondiente corrió en el turno (anti-alucinación: el modelo a veces "dice"
# que agendó/canceló/envió el correo sin llamar la tool). Mismo mecanismo que el
# `crm_completion_check` de pack-commerce.
# _TOOL_NAME_KEY: literal "_tool_name" (los packs no importan de core.orchestrator).
_COMPLETION_CLAIMS: dict[str, tuple[re.Pattern[str], set[str]]] = {
    "agendada": (
        re.compile(
            r"qued\w*\s+(?:tu\s+)?(?:demo|cita\w*)?\s*agendad\w*|"
            r"(?:ya\s+)?te\s+agend\w+\s+(?:la\s+)?(?:demo|cita)",
            re.IGNORECASE,
        ),
        {"create_calendar_event"},
    ),
    "reagendada": (
        re.compile(r"qued\w*\s+(?:re)agendad\w*|reprogramad\w*|mov\w+\s+(?:la\s+)?cita", re.IGNORECASE),
        {"update_calendar_event"},
    ),
    "cancelada": (
        re.compile(r"qued\w*\s+cancelad\w*|cancel\w+\s+(?:la\s+)?cita|qued\w*\s+anulad\w*", re.IGNORECASE),
        {"cancel_calendar_event"},
    ),
    "correo_enviado": (
        re.compile(
            r"envi\w*\s+(?:la\s+)?(?:confirmaci[oó]n|el\s+correo|un\s+correo)|"
            r"te\s+envi\w*\s+la\s+confirmaci[oó]n",
            re.IGNORECASE,
        ),
        {"send_email"},
    ),
    "listado": (
        re.compile(
            r"no\s+ten\w*\s+(?:ninguna\s+|ningunas\s+)?citas?\s+agendadas|"
            r"ten\w*\s+\d+\s+(?:citas?|demos?)\s+agendadas",
            re.IGNORECASE,
        ),
        {"list_client_appointments"},
    ),
}


class GoogleCalendarModule(BaseModule):
    id = PLUGIN_ID
    name = "Calendario y citas"
    version = "2.1.0"
    description = "Agendamiento con agenda interna (sin Google) o Google Calendar (ADR-0084)"
    requires_config = True
    config_schema = CalendarConfig
    # Submenú al habilitar el pack (ADR-0061/0063).
    admin_menu: list[dict[str, str]] = [
        {"label": "Conexión", "path": "/calendar/", "section": "Configuración", "section_key": "pack_calendar:conexion"},
        {"label": "Horarios y calendario", "path": "/calendar/configuracion", "section": "Configuración", "section_key": "pack_calendar:configuracion"},
        {"label": "Tipos de cita", "path": "/calendar/tipos", "section": "Configuración", "section_key": "pack_calendar:tipos"},
        {"label": "Citas", "path": "/calendar/citas", "section": "Operación", "section_key": "pack_calendar:citas"},
    ]
    admin_router = calendar_admin_router

    def admin_api_routes(self) -> list[object]:
        """API admin consumida por el panel nuevo (ADR-0079)."""
        return [calendar_admin_api_router]
    declared_skills: list[type] = [CalendarSkill]

    def on_activate(self, host: MCPHost) -> None:
        config = self.config
        if not isinstance(config, CalendarConfig):
            from core.shared.exceptions import PluginConfigError

            raise PluginConfigError(PLUGIN_ID, "Config inválida o ausente")
        host.register_skill(CalendarSkill())
        host.register_guardrail("calendar_completion_check", self._completion_guardrail, priority=6)

    async def _completion_guardrail(
        self, response_text: str, tool_results: list[dict[str, Any]]
    ) -> GuardrailResult:
        """Bloquea afirmaciones de acciones de citas sin la tool correspondiente.

        Si el LLM dice que agendó/reagendó/canceló una cita, envió el correo de
        confirmación, o enumera las citas del cliente, la tool que respalda esa
        afirmación DEBE haber corrido en este turno; si no, se reintenta con la
        instrucción correctiva (y si sigue fallando, el orchestrator escala) --
        así el cliente nunca recibe una confirmación falsa.
        """
        called_tools = {
            str(result.get("_tool_name") or "") for result in tool_results if isinstance(result, dict)
        }
        for label, (pattern, required_tools) in _COMPLETION_CLAIMS.items():
            if pattern.search(response_text) and not (called_tools & required_tools):
                return GuardrailResult(
                    passed=False,
                    reason=f"Afirmación de '{label}' sin la tool correspondiente en este turno",
                    retry_instruction=(
                        f"Dijiste que se completó '{label}' pero no llamaste la herramienta "
                        f"correspondiente ({', '.join(sorted(required_tools))}) en este turno. "
                        "Llamala ahora mismo, o si no podés completarla de verdad, decilo "
                        "explícitamente en vez de prometer algo que no vas a cumplir."
                    ),
                )
        return GuardrailResult(passed=True)

    def extra_admin_routers(self) -> list[APIRouter]:
        """Router OAuth con la URL vieja (redirect URI de Google Console)."""
        return []

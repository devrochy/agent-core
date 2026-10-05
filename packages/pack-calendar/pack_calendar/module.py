"""GoogleCalendarModule: pack-calendar v2 (ADR-0069/0070).

- `admin_router` = panel propio `/calendar/*` con navegación por secciones
  (ADR-0061/0063): Conexión · Horarios y calendario · Tipos de cita · Citas.
- El router OAuth conserva su URL `/admin/plugins/pack_calendar/oauth[/callback]`
  (redirect URI registrado en Google Console, ADR-0070), por eso va como
  `extra_admin_routers`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

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
    # Afirmar que el cliente YA tiene una cita (sin crearla en este turno) exige
    # verificarlo con list_client_appointments (corrección post-prueba 2026-10-05:
    # el asistente afirmó "Tenés tu demo agendada..." sin listar y se contradijo).
    "cita_existente": (
        re.compile(
            r"ten[ée]s\s+(?:una\s+|tu\s+)?(?:demo|cita)\s+agendad|"
            r"ya\s+ten[ée]s\s+(?:una\s+|tu\s+)?(?:demo|cita)|"
            r"figura\s+agendad|aparece\s+agendad",
            re.IGNORECASE,
        ),
        {"list_client_appointments"},
    ),
}

# Tope de líneas de claims custom (mismo criterio que pack-commerce: este
# guardrail corre en CADA turno, una config enorme degradaría la latencia).
_MAX_CUSTOM_COMPLETION_CLAIM_LINES = 100


def _parse_custom_completion_claims(raw: str) -> dict[str, tuple[re.Pattern[str], set[str]]]:
    """Parsea `CalendarConfig.completion_claims_custom` (ADR-0025/#157):
    `etiqueta: frase uno | frase dos => tool_uno, tool_dos` por línea. Las
    frases se escapan (`re.escape`), nunca regex crudo (anti-ReDoS). Una línea
    mal formada se ignora con warning, nunca rompe el guardrail (fail-open).
    """
    result: dict[str, tuple[re.Pattern[str], set[str]]] = {}
    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        if lineno > _MAX_CUSTOM_COMPLETION_CLAIM_LINES:
            log.warning("calendar_claims_custom_truncado", limite=_MAX_CUSTOM_COMPLETION_CLAIM_LINES)
            break
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line or "=>" not in line:
            log.warning("calendar_claim_custom_malformado", line=lineno)
            continue
        label, _, rest = line.partition(":")
        phrases_part, _, tools_part = rest.partition("=>")
        label = label.strip()
        phrases = [p.strip() for p in phrases_part.split("|") if p.strip()]
        tools = {t.strip() for t in tools_part.split(",") if t.strip()}
        if not label or not phrases or not tools:
            log.warning("calendar_claim_custom_incompleto", line=lineno)
            continue
        result[label] = (
            re.compile("|".join(re.escape(p) for p in phrases), re.IGNORECASE),
            tools,
        )
    return result


class GoogleCalendarModule(BaseModule):
    id = PLUGIN_ID
    name = "Calendario y citas"
    version = "2.1.0"
    description = "Agendamiento con agenda interna (sin Google) o Google Calendar (ADR-0084)"
    display_name = "Agenda"
    category = "Agenda"
    min_plan = "free"
    required_connections: ClassVar[list[str]] = ["google_calendar"]
    action_summary: ClassVar[list[str]] = ["Consultar disponibilidad", "Agendar citas", "Recordatorios"]
    setup_guide = "Define tu horario de atención y los tipos de cita (o conecta Google Calendar)."
    requires_config = True
    config_schema = CalendarConfig
    # Submenú al habilitar el pack (ADR-0061/0063).
    admin_menu: list[dict[str, str]] = [
        {"label": "Conexión", "path": "/calendar/", "section": "Configuración", "section_key": "pack_calendar:conexion"},
        {"label": "Horarios y calendario", "path": "/calendar/configuracion", "section": "Configuración", "section_key": "pack_calendar:configuracion"},
        {"label": "Tipos de cita", "path": "/calendar/tipos", "section": "Configuración", "section_key": "pack_calendar:tipos"},
        {"label": "Bloqueos", "path": "/calendar/bloqueos", "section": "Configuración", "section_key": "pack_calendar:bloqueos"},
        {"label": "Citas", "path": "/calendar/citas", "section": "Operación", "section_key": "pack_calendar:citas"},
    ]
    # Sub-sección de permiso sin pantalla: quien la tenga recibe copia de los
    # correos de agendamiento/reagenda/cancelación (HU-CAL-02).
    section_keys: ClassVar[list[str]] = ["pack_calendar:notificaciones"]
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

    async def _resolved_completion_claims(self) -> dict[str, tuple[re.Pattern[str], set[str]]]:
        """Combina `_COMPLETION_CLAIMS` (incorporados) con la config de ESTE
        tenant (ADR-0025/#157): quita los deshabilitados
        (`completion_claims_disabled`), agrega/reemplaza los custom
        (`completion_claims_custom`). Fail-open ante config ausente/inválida.
        """
        from core.plugin_manager import plugin_manager

        claims = dict(_COMPLETION_CLAIMS)
        try:
            config = await plugin_manager.load_config(PLUGIN_ID)
        except Exception:
            config = None
        if config is None:
            return claims
        disabled = {
            n.strip() for n in (config.completion_claims_disabled or "").split(",") if n.strip()
        }
        for name in disabled:
            claims.pop(name, None)
        claims.update(_parse_custom_completion_claims(config.completion_claims_custom or ""))
        return claims

    async def _completion_guardrail(
        self, response_text: str, tool_results: list[dict[str, Any]]
    ) -> GuardrailResult:
        """Bloquea afirmaciones de acciones de citas sin la tool correspondiente.

        Si el LLM dice que agendó/reagendó/canceló una cita, envió el correo de
        confirmación, o enumera las citas del cliente, la tool que respalda esa
        afirmación DEBE haber corrido en este turno **y con éxito**; si no, se
        reintenta con la instrucción correctiva (y si sigue fallando, el
        orchestrator escala) -- así el cliente nunca recibe una confirmación falsa.
        Los claims son los incorporados + los configurados/desactivados por el
        tenant (ADR-0025/#157).

        Corrección post-prueba 2026-10-05: se cuentan solo resultados del turno
        ACTUAL (`_current_turn`) y exitosos (`_ok`), mismo criterio que el
        guardrail de commerce (HU-COM-02). Un `create_calendar_event` de un turno
        anterior, o uno que devolvió `needs_data`/error, NO respalda "quedó
        agendada".
        """
        called_tools = {
            str(result.get("_tool_name") or "")
            for result in tool_results
            if isinstance(result, dict)
            and result.get("_current_turn")
            and result.get("_ok")
        }
        claims = await self._resolved_completion_claims()
        for label, (pattern, required_tools) in claims.items():
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

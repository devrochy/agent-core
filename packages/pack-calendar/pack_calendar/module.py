"""GoogleCalendarModule: pack-calendar v2 (ADR-0069/0070).

- `admin_router` = panel propio `/calendar/*` con navegación por secciones
  (ADR-0061/0063): Conexión · Horarios y calendario · Tipos de cita · Citas.
- El router OAuth conserva su URL `/admin/plugins/pack_calendar/oauth[/callback]`
  (redirect URI registrado en Google Console, ADR-0070), por eso va como
  `extra_admin_routers`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from fastapi import APIRouter

import pack_calendar.jobs  # noqa: F401  # registra el @register_cron (HU-SH-02)
from core.mcp.types import BaseModule, GuardrailResult
from core.shared.logger import get_logger
from pack_calendar import google_api
from pack_calendar.admin import router as calendar_admin_router
from pack_calendar.admin_api import router as calendar_admin_api_router
from pack_calendar.config import CalendarConfig
from pack_calendar.skills import CalendarSkill

if TYPE_CHECKING:
    import uuid

    from core.mcp.host import MCPHost
    from core.primeros_pasos.contrato import ContenidoCargado

PLUGIN_ID = "pack_calendar"

log = get_logger(__name__)

# OAuth: conserva la URL vieja (ver docstring).


# Afirmaciones del LLM sobre acciones de citas que SOLO son válidas con evidencia
# real del turno (anti-alucinación: el modelo a veces "dice" que agendó/canceló/
# envió el correo sin que haya pasado). Mismo mecanismo que el
# `crm_completion_check` de pack-commerce.
#
# HU-CAL-05 (D-SPEC-24-8): cada claim separa la AFIRMACIÓN ("quedó agendada",
# "moví tu demo") de la NEGACIÓN ("no pude agendarla"). Una afirmación exige el
# `status` de éxito de la tool en este turno; una negación, que la acción se haya
# intentado. Los verbos van en pasado o presente afirmativo: "¿te agendo la
# demo?", "¿querés que cancele la cita?" o "quedaría agendada" son preguntas o
# propuestas, no afirmaciones (en la prueba real disparaban reintentos y
# escalamientos).
# _TOOL_NAME_KEY: literal "_tool_name" (los packs no importan de core.orchestrator).
_QUEDO = r"qued(?:ó|o|a|an|aron)\b"


# "Disponibilidad" genérica: sin un dato de agenda cerca puede ser stock o cupo de un producto.
_DISPONIBLE_GENERICO = (
    r"(?:\b(?:est[áa]|queda|quedan)\s+libres?\b|"
    r"\b(?:hay|tengo|tenemos)\s+(?:disponibilidad|cupos?|espacio)\b)"
)
# Dato de agenda: una hora, un día, una fecha o la palabra de la cita.
_AGENDA = (
    r"(?:\b(?:a\s+las?|desde\s+las?|entre\s+las?)\s+\d|\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.\s?m\.|p\.\s?m\.|hs?)\b|"
    r"\b\d{1,2}:\d{2}\b|\b(?:horas?|horarios?|cita|citas|demo|demos|reuni[oó]n|agenda|turno|turnos|"
    r"ma[ñn]ana|tarde|semana|fecha|lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\b)"
)
# Los dos en la misma oración (acotado: sin backtracking largo).
_MISMA_ORACION = r"[^.!?\n]{0,120}?"


@dataclass(frozen=True)
class _Claim:
    tools: frozenset[str]
    affirm: re.Pattern[str]
    deny: re.Pattern[str] | None = None


_CLAIMS: dict[str, _Claim] = {
    "agendada": _Claim(
        tools=frozenset({"create_calendar_event"}),
        affirm=re.compile(
            rf"\b{_QUEDO}\s+(?:tu\s+|la\s+)?(?:demo|cita\w*)?\s*agendad\w*|"
            r"\b(?:te\s+)?agendé\s+(?:la\s+|tu\s+)?(?:demo|cita)",
            re.IGNORECASE,
        ),
        deny=re.compile(
            r"no\s+pud\w*\s+(?:agend\w*(?:la|lo)\b|agendar\s+(?:la|tu|esa)\s+(?:cita|demo))|"
            rf"no\s+{_QUEDO}\s+(?:tu\s+|la\s+)?(?:demo|cita\w*)?\s*agendad\w*",
            re.IGNORECASE,
        ),
    ),
    "reagendada": _Claim(
        tools=frozenset({"update_calendar_event"}),
        affirm=re.compile(
            rf"\b{_QUEDO}\s+reagendad\w*|\b(?:{_QUEDO}|fue|fueron)\s+reprogramad\w*|"
            r"\bmov[íi]\w*\s+(?:tu\s+|la\s+|esa\s+)?(?:demo|cita)|"
            rf"\b{_QUEDO}\s+movid\w*|\b(?:ya\s+)?(?:te\s+)?la\s+mov[íi]\b",
            re.IGNORECASE,
        ),
        deny=re.compile(
            r"no\s+pud\w*\s+(?:reagend\w*|reprogram\w*|mover\w*|mov[íi]\w*)|"
            r"no\s+(?:la\s+)?(?:encontr|hall)\w+.{0,60}(?:reagend\w*|mover|mov[íi]\w*)|"
            rf"no\s+{_QUEDO}\s+reagendad\w*",
            re.IGNORECASE,
        ),
    ),
    "cancelada": _Claim(
        tools=frozenset({"cancel_calendar_event"}),
        affirm=re.compile(
            rf"\b{_QUEDO}\s+(?:cancelad|anulad)\w*|\b(?:fue|fueron)\s+cancelad\w*|"
            r"\bcancel(?:é|ó|amos)\s+(?:la\s+|tu\s+|esa\s+)?(?:cita|demo)",
            re.IGNORECASE,
        ),
        deny=re.compile(
            r"no\s+pud\w*\s+(?:cancel\w*(?:la|lo)\b|cancelar\s+(?:la|tu|esa)\s+(?:cita|demo))|"
            r"no\s+(?:la\s+)?(?:encontr|hall|figur|aparec)\w+.{0,60}cancel\w*|"
            rf"no\s+{_QUEDO}\s+cancelad\w*",
            re.IGNORECASE,
        ),
    ),
    "correo_enviado": _Claim(
        tools=frozenset({"send_email"}),
        affirm=re.compile(
            r"\b(?:te\s+)?(?:envié|enviamos|envió|se\s+envió|mandé|mandamos)\s+"
            r"(?:la\s+|el\s+|un\s+)?(?:confirmaci[oó]n|correo|email|mail)|"
            r"\b(?:te\s+)?(?:va\s+a\s+llegar|llegar[áa]|lleg[óo])\s+"
            r"(?:la\s+|el\s+|un\s+)?(?:confirmaci[oó]n|correo|email|mail)",
            re.IGNORECASE,
        ),
    ),
    # E2E HU-CAL-06: "A las 3:00 pm está libre" sin consultar. Afirmar que un
    # horario está libre u ocupado exige la consulta (o el intento de agendar/mover,
    # que valida el horario en el servidor).
    "disponibilidad": _Claim(
        tools=frozenset({"check_availability", "find_next_available"}),
        # Acotado a horarios: "el plan Basic está disponible" es venta, no agenda. Las formas
        # genéricas ("tenemos disponibilidad", "hay cupos", "queda libre") solo cuentan con un
        # dato de agenda en la misma oración: en un negocio que también vende, "¡Sí, tenemos
        # disponibilidad! La hamburguesa cuesta…" es inventario y escalaba el pedido (pass^4 de
        # los procedimientos, 2026-10-18).
        affirm=re.compile(
            r"\bhorarios?\s+(?:libres?|disponibles?)\b|"
            r"\b(?:ese|este|el)\s+horario\s+(?:ya\s+)?(?:est[áa]|queda)\s+"
            r"(?:ocupad|tomad|disponible|libre)\w*|"
            rf"{_AGENDA}{_MISMA_ORACION}{_DISPONIBLE_GENERICO}|"
            rf"{_DISPONIBLE_GENERICO}{_MISMA_ORACION}{_AGENDA}",
            re.IGNORECASE,
        ),
    ),
    "listado": _Claim(
        tools=frozenset({"list_client_appointments"}),
        affirm=re.compile(
            r"no\s+ten\w*\s+(?:ninguna\s+|ningunas\s+)?citas?\s+agendadas|"
            r"ten\w*\s+\d+\s+(?:citas?|demos?)\s+agendadas|"
            r"no\s+(?:me\s+)?(?:aparece|figura|encontr\w+).{0,50}(?:citas?|demo)|"
            # E2E HU-CAL-06: enumeró citas de memoria sin listar.
            r"\b(?:estas|esta)\s+(?:son|es)\s+tus\s+(?:citas|demos)\b|"
            r"\btus\s+(?:citas|demos)\s+(?:agendadas|activas|programadas)|"
            r"\b(?:es\s+)?la\s+[úu]nica\s+(?:cita\s+|demo\s+)?(?:activa|agendada|programada)|"
            r"\bten[ée]s\s+(?:una\s+sola|solo\s+una|s[óo]lo\s+\d+|\d+)\s+(?:citas?|demos?)",
            re.IGNORECASE,
        ),
    ),
    # Afirmar que el cliente YA tiene una cita (sin crearla en este turno) exige
    # verificarlo con list_client_appointments (corrección post-prueba 2026-10-05:
    # el asistente afirmó "Tenés tu demo agendada..." sin listar y se contradijo).
    "cita_existente": _Claim(
        tools=frozenset({"list_client_appointments"}),
        affirm=re.compile(
            r"ten[ée]s\s+(?:una\s+|tu\s+)?(?:demo|cita)\s+agendad|"
            r"ya\s+ten[ée]s\s+(?:una\s+|tu\s+)?(?:demo|cita)|"
            r"figura\s+agendad|aparece\s+agendad|"
            # E2E HU-CAL-06: describir una cita ("tu demo está agendada para el
            # jueves") también es afirmar su estado.
            r"\btu\s+(?:demo|cita)\s+(?:est[áa]|sigue)\s+(?:agendad|programad|para\s+el)|"
            r"\btu\s+(?:demo|cita)\s+es\s+(?:el|este|esta|ma[ñn]ana|hoy)\b",
            re.IGNORECASE,
        ),
    ),
}

# Vista compatible (etiqueta → patrón que detecta el claim, tools): la usan la
# config por tenant (deshabilitar/reemplazar) y el DSL de claims custom.
_COMPLETION_CLAIMS: dict[str, tuple[re.Pattern[str], set[str]]] = {
    label: (
        re.compile(
            "|".join(f"(?:{p.pattern})" for p in (claim.affirm, claim.deny) if p is not None),
            re.IGNORECASE,
        ),
        set(claim.tools),
    )
    for label, claim in _CLAIMS.items()
}

# Acciones cuyo resultado trae `email_sent` (correo determinista, HU-CAL-02).
_EMAILING_TOOLS = frozenset({"create_calendar_event", "update_calendar_event", "cancel_calendar_event"})
_DONT_MENTION = " Reescribí tu respuesta para el cliente sin mencionar esta instrucción."
# Una pregunta en español va entre "¿" y "?": se descarta antes de buscar afirmaciones.
_QUESTION = re.compile(r"¿[^¿?]*\?")


def _of(results: list[dict[str, Any]], *names: str) -> list[dict[str, Any]]:
    return [r for r in results if str(r.get("_tool_name") or "") in names]


def _listed_ok(results: list[dict[str, Any]]) -> bool:
    return any(r.get("_ok") for r in _of(results, "list_client_appointments"))


def _affirmation_backed(label: str, results: list[dict[str, Any]]) -> bool:
    """Evidencia REAL de una afirmación en los resultados del turno actual."""
    if label == "agendada":
        creates = _of(results, "create_calendar_event")
        return any(r.get("status") == "created" for r in creates) or (not creates and _listed_ok(results))
    if label == "reagendada":
        updates = _of(results, "update_calendar_event")
        return any(r.get("status") == "updated" for r in updates) or (not updates and _listed_ok(results))
    if label == "cancelada":
        return any(r.get("status") == "cancelled" for r in _of(results, "cancel_calendar_event"))
    if label == "correo_enviado":
        return any(r.get("_ok") for r in _of(results, "send_email")) or any(
            r.get("email_sent") is True for r in _of(results, *_EMAILING_TOOLS)
        )
    if label == "disponibilidad":
        return any(r.get("_ok") for r in _of(results, "check_availability", "find_next_available")) or bool(
            _of(results, "create_calendar_event", "update_calendar_event")
        )
    if label == "cita_existente":
        # Describir la cita que se acaba de crear o mover en este turno está respaldado.
        return _listed_ok(results) or any(
            r.get("status") in ("created", "updated")
            for r in _of(results, "create_calendar_event", "update_calendar_event")
        )
    return _listed_ok(results)  # listado


def _denial_backed(label: str, results: list[dict[str, Any]]) -> bool:
    """Decir "no pude…" exige haberlo intentado en el turno (o, para mover o
    cancelar, haber verificado con el listado que la cita no está)."""
    claim = _CLAIMS[label]
    if _of(results, *claim.tools):
        return True
    return label in ("reagendada", "cancelada") and _listed_ok(results)


def _builtin_verdict(label: str, text: str, results: list[dict[str, Any]]) -> GuardrailResult | None:
    claim = _CLAIMS[label]
    tools = ", ".join(sorted(claim.tools))
    if claim.deny is not None and claim.deny.search(text) and not _denial_backed(label, results):
        return GuardrailResult(
            passed=False,
            reason=f"Negación de '{label}' sin haberlo intentado en este turno",
            retry_instruction=(
                f"Tu respuesta dice que no se pudo completar '{label}', pero no lo intentaste en "
                f"este turno. Llamá la herramienta correspondiente ({tools}) y contá lo que devuelva."
                + _DONT_MENTION
            ),
        )
    affirm_text = claim.deny.sub(" ", text) if claim.deny is not None else text
    if not claim.affirm.search(affirm_text) or _affirmation_backed(label, results):
        return None

    if label == "correo_enviado":
        unsent = [r for r in _of(results, *_EMAILING_TOOLS) if r.get("email_sent") is False]
        if unsent:
            reason = str(unsent[-1].get("email_reason") or "error_envio")
            retry = (
                f"El correo de confirmación no se pudo enviar (motivo: {reason}). NO digas que "
                "lo enviaste: contá lo que sí se hizo y que el correo no salió."
            )
        else:
            retry = (
                "Tu respuesta dice que se envió un correo, pero en este turno no se envió ninguno. "
                "No lo afirmes: el correo lo envía el sistema solo al agendar, reagendar o "
                "cancelar una cita."
            )
    elif attempted := _of(results, *claim.tools):
        last = attempted[-1]
        detail = str(last.get("error") or last.get("message") or last.get("reason") or "").strip()
        retry = (
            f"La herramienta no completó '{label}': devolvió \"{detail[:200]}\". "
            "NO afirmes que se completó: seguí esa instrucción (por ejemplo, "
            "pedirle al cliente el dato que falta) y recién después confirmá."
        )
    elif label == "disponibilidad":
        retry = (
            "Tu respuesta afirma qué horarios están libres u ocupados pero no consultaste la "
            "disponibilidad en este turno. Llamá check_availability con la fecha (o "
            "find_next_available si no hay fecha) y ofrecé solo los horarios que devuelva."
        )
    else:
        retry = (
            f"Tu respuesta afirma el resultado de '{label}' pero no llamaste la "
            f"herramienta correspondiente ({tools}) en este turno. Llamala ahora mismo y "
            "contá lo que devuelva, o si no podés completarla de verdad, decilo sin "
            "prometer ni dar por fallido algo que no intentaste."
        )
    return GuardrailResult(
        passed=False,
        reason=f"Afirmación de '{label}' sin evidencia de la tool en este turno",
        retry_instruction=retry + _DONT_MENTION,
    )


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
    version = "2.2.0"
    description = "Agendamiento con agenda interna (sin Google) o Google Calendar (ADR-0084)"
    display_name = "Agenda"
    category = "Agenda"
    min_plan = "free"
    required_connections: ClassVar[list[str]] = ["google_calendar"]
    # Salida a red de las tools y del panel (ADR-0052): solo las APIs de Google.
    network_allowlist: ClassVar[tuple[str, ...]] = google_api.NETWORK
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
        # HU-SH-02: el equipo se entera de cada cita nueva, reagendada o cancelada.
        from core.shared.events import ON_APPOINTMENT_CHANGED

        host.subscribe(ON_APPOINTMENT_CHANGED, _on_cita_cambiada)
        # HU-AOS-08: procedimiento `agendar_cita` y sus resolvers (solo los usa el motor, detrás
        # de `tenants.aos_procedures`).
        from pack_calendar.procedimientos import registrar

        registrar(host, PLUGIN_ID)

    async def seed_data(self, tenant_id: uuid.UUID, data: dict[str, Any]) -> list[str]:
        """Datos de ejemplo de la agenda (ADR-0083): tipos de cita, citas y bloqueos.
        Idempotente (ver `pack_calendar.seed`)."""
        from pack_calendar.seed import sembrar_agenda

        return await sembrar_agenda(tenant_id, data)

    async def contenido_cargado(self, tenant_id: uuid.UUID) -> ContenidoCargado:
        """Tipos de cita activos (los servicios que se agendan) para el paso «catálogo o
        servicios» de los primeros pasos (HU-SH-06, ADR-0124). Solo lee."""
        from sqlalchemy import func, select

        from core.db.session import get_session
        from core.primeros_pasos.contrato import ContenidoCargado
        from core.shared.tenant import tenant_scope
        from pack_calendar.models import AppointmentType

        async with tenant_scope(tenant_id):
            async with get_session() as session:
                cantidad, primero = (
                    await session.execute(
                        select(func.count(AppointmentType.id), func.min(AppointmentType.created_at)).where(
                            AppointmentType.tenant_id == tenant_id,
                            AppointmentType.active.is_(True),
                            AppointmentType.deleted_at.is_(None),
                        )
                    )
                ).one()
        return ContenidoCargado(
            tipo="servicios", cantidad=int(cantidad or 0), primero_en=primero, seccion="pack_calendar:tipos"
        )

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
        orchestrator escala o responde neutral según `guardrail_fail_action`,
        ADR-0092) -- así el cliente nunca recibe una confirmación falsa.
        Los claims son los incorporados + los configurados/desactivados por el
        tenant (ADR-0025/#157).

        Solo cuentan los resultados del turno ACTUAL (`_current_turn`, HU-COM-02).
        HU-CAL-05: la evidencia es el `status` real (`created`, `updated`,
        `cancelled`, `email_sent`), no la mera ausencia de error: un create
        `rejected` o un cancel `not_found` no respaldan "quedó agendada/cancelada".
        """
        current = [r for r in tool_results if isinstance(r, dict) and r.get("_current_turn")]
        # Las preguntas ("¿Querés que revise tus citas agendadas?") no afirman nada.
        statements = _QUESTION.sub(" ", response_text)
        claims = await self._resolved_completion_claims()
        for label, claim in claims.items():
            if label in _COMPLETION_CLAIMS and claim is _COMPLETION_CLAIMS[label]:
                verdict = _builtin_verdict(label, statements, current)
                if verdict is not None:
                    return verdict
                continue
            # Claim propio del tenant (DSL, ADR-0025/#157): basta con que la tool
            # haya corrido con éxito en este turno.
            pattern, required_tools = claim
            called = {str(r.get("_tool_name") or "") for r in current if r.get("_ok")}
            if pattern.search(response_text) and not (called & required_tools):
                return GuardrailResult(
                    passed=False,
                    reason=f"Afirmación de '{label}' sin la tool correspondiente en este turno",
                    retry_instruction=(
                        f"Tu respuesta afirma el resultado de '{label}' pero no llamaste la "
                        f"herramienta correspondiente ({', '.join(sorted(required_tools))}) en este "
                        "turno. Llamala ahora mismo y contá lo que devuelva." + _DONT_MENTION
                    ),
                )
        return GuardrailResult(passed=True)

    def extra_admin_routers(self) -> list[APIRouter]:
        """Router OAuth con la URL vieja (redirect URI de Google Console)."""
        return []


async def _on_cita_cambiada(**payload: Any) -> None:
    """Outbox → push al asignado de la cita (o a los dueños)."""
    from pack_calendar.demo import avisar_al_equipo

    await avisar_al_equipo(
        str(payload.get("appointment_id")),
        str(payload.get("accion") or ""),
        str(payload.get("titulo") or ""),
        payload.get("inicio"),
    )

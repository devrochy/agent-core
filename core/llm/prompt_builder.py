"""SystemPromptBuilder: ENSAMBLADOR de bloques del system prompt (Sprint 2, ADR-0014).

Este módulo NO contiene texto de instrucciones al modelo: todo el contenido vive en
playbooks (`.md`, allowlist de `AgentSpec.playbooks`) y en datos del tenant. Aquí solo
se ensambla:

  1. Los playbooks del tenant en el orden del índice `AgentSpec.playbooks`,
     con sustitución de placeholders ({assistant_name}, {business_description},
     {language}).
  2. Bloques dinámicos del turno (voz de marca, skills activas, perfil y memorias)
     que cambian por turno — van al final para no invalidar el cache de los bloques
     estables (docs/PROMPT-CACHING-GUIDE.md). La fecha y hora actual del negocio es
     la última parte y solo trae el dato; cómo usarla lo dice el playbook
     `reglas-operacion` (ADR-0091).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentcore_contracts import AgentSpec

from core.shared.logger import get_logger

log = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Bogota"
# Encabezado del dato de fecha y hora (ADR-0091). Lo usa también la normalización de las
# grabaciones de evals (`core/llm/cassette.py`) para ignorar la parte que cambia cada minuto.
CURRENT_DATETIME_HEADER = "## Fecha y hora actual"

_NEXT_DAYS = {"es": "Próximos días", "en": "Next days", "pt": "Próximos dias"}
_TOMORROW = {"es": "mañana", "en": "tomorrow", "pt": "amanhã"}
# Nombres de día (lunes=0) y mes (enero=1) por idioma: datos, no instrucciones.
_WEEKDAYS = {
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "pt": ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"),
}
_MONTHS = {
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
           "septiembre", "octubre", "noviembre", "diciembre"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"),
    "pt": ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
           "setembro", "outubro", "novembro", "dezembro"),
}


@dataclass
class PromptContext:
    assistant_name: str
    business_description: str
    language: str = "es"
    tone: str = "cercano"
    warmth: int = 3
    emojis: str = "sparse"
    max_length: int = 500
    brand_voice: str = ""
    flow_summary: str = ""            # flujo de atención del tenant (ADR-0054)
    active_skills_summary: str = ""   # descripción de las skills activas del turno
    recalled_memories: list[str] = field(default_factory=list)  # Capa 3 (RAG)
    entity_profile: str = ""          # Capa 2 (perfil del cliente)
    referencias: list[str] = field(default_factory=list)  # ids vigentes de la conversación (ADR-0130)
    timezone: str = DEFAULT_TIMEZONE  # zona del negocio (ADR-0091)
    now: datetime | None = None       # None = ahora; inyectable en tests

    @property
    def template_vars(self) -> dict[str, str]:
        language_names = {"es": "español", "en": "inglés", "pt": "portugués"}
        emoji_names = {
            "none": "no uses emojis",
            "sparse": "usa emojis con moderación",
            "free": "podés usar emojis libremente",
        }
        return {
            "assistant_name": self.assistant_name,
            "business_description": self.business_description,
            "language": language_names.get(self.language, self.language),
            # v2.0.0 (ADR-0050): la persona llega a los playbooks como variables,
            # así el tono/calidez/emojis configurados tienen efecto real.
            "tone": self.tone,
            "warmth": str(self.warmth),
            "emojis": emoji_names.get(self.emojis, self.emojis),
            "max_length": str(self.max_length),
        }


def _current_datetime_part(ctx: PromptContext) -> str:
    """Fecha y hora actual en la zona del negocio, como dato (ADR-0091)."""
    try:
        tz = ZoneInfo(ctx.timezone or DEFAULT_TIMEZONE)
        tz_name = ctx.timezone or DEFAULT_TIMEZONE
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("prompt_timezone_invalid", timezone=ctx.timezone)
        tz, tz_name = ZoneInfo(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE
    now = ctx.now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(tz)
    lang = ctx.language if ctx.language in _WEEKDAYS else "es"
    weekday = _WEEKDAYS[lang][local.weekday()]
    month = _MONTHS[lang][local.month - 1]
    hour = local.strftime("%H:%M")
    if lang == "en":
        line = f"Today is {weekday}, {month} {local.day}, {local.year} — {hour} ({tz_name})."
    elif lang == "pt":
        line = f"Hoje é {weekday}, {local.day} de {month} de {local.year} — {hour} ({tz_name})."
    else:
        line = f"Hoy es {weekday}, {local.day} de {month} de {local.year} — {hour} ({tz_name})."
    return f"{CURRENT_DATETIME_HEADER}\n{line}\n{_next_days_line(local.date(), lang)}"


def _next_days_line(today: date, lang: str) -> str:
    """Los 7 días siguientes con su fecha (E2E HU-CAL-06: "el próximo miércoles"
    se resolvía a la semana equivocada; con la lista el modelo no hace cuentas)."""
    days = []
    for offset in range(1, 8):
        day = today + timedelta(days=offset)
        weekday = _WEEKDAYS[lang][day.weekday()]
        month = _MONTHS[lang][day.month - 1]
        label = f"{weekday}, {month} {day.day}" if lang == "en" else (
            f"{weekday}, {day.day} de {month}" if lang == "pt" else f"{weekday} {day.day} de {month}"
        )
        days.append(f"{_TOMORROW[lang]} {label}" if offset == 1 else label)
    return f"{_NEXT_DAYS[lang]}: " + " · ".join(days) + "."


async def _render_playbook(
    spec: AgentSpec, ref_name: str, vars_: dict[str, str], tenant_slug: str | None
) -> str | None:
    """Carga el playbook y sustituye placeholders. None si no está en el índice."""
    from core.agents.playbooks import load_playbook

    content = await load_playbook(spec, ref_name, tenant_slug=tenant_slug)
    if content is None:
        return None
    try:
        return content.format(**vars_)
    except KeyError:
        # playbook con placeholder no provisto: se incluye tal cual
        return content


async def build_system_blocks(spec: AgentSpec, ctx: PromptContext) -> list[str]:
    """Ensambla los bloques del system prompt desde el spec + playbooks + turno."""
    import uuid

    blocks: list[str] = []

    # Override por tenant (ADR-0026): se resuelve UNA VEZ acá, no dentro de
    # cada llamada a load_playbook -- este loop puede pedir 4-6 playbooks por
    # turno, y una query de slug por cada uno sería un N+1 real.
    tenant_slug: str | None = None
    try:
        from core.shared.tenant import tenant_slug as _tenant_slug

        tenant_slug = await _tenant_slug(uuid.UUID(spec.tenantId))
    except Exception:
        # `tenantId` inválido (specs de test) o la BD no responde: el
        # override es una capa extra, no la fuente de verdad -- degradar a
        # "sin override" en vez de tumbar el ensamblado del prompt entero.
        # Se loggea igual para que un fallo persistente de BD no quede
        # invisible (antes era un `pass` silencioso).
        log.warning("tenant_slug_resolve_failed", tenant_id=spec.tenantId, exc_info=True)
    if tenant_slug is None:
        # "" (no None) le dice a load_playbook "ya se intentó, no hay slug"
        # -- evita que cada llamada del loop de abajo reintente la misma
        # query si la resolución de arriba falló (ej. BD caída).
        tenant_slug = ""

    # 1. Playbooks del tenant, en el orden del índice (identidad, tono, reglas...)
    for ref in spec.playbooks:
        rendered = await _render_playbook(spec, ref.name, ctx.template_vars, tenant_slug)
        if rendered:
            blocks.append(rendered)

    # 2. Voz de marca (dato del tenant, estable por negocio)
    if ctx.brand_voice:
        blocks.append(f"## Voz de marca\n{ctx.brand_voice}")

    # 2b. Flujo de atención (ADR-0054): estable por tenant, va antes de los bloques
    # dinámicos para no invalidar el cache. Es la "base de comportamiento" del pack.
    if ctx.flow_summary:
        blocks.append(
            "## Flujo de atención\n"
            "Seguí estas etapas, en orden, según lo que pida el cliente. "
            "Cada etapa lista las herramientas que corresponden a ese momento:\n"
            f"{ctx.flow_summary}"
        )

    # 3. Contexto dinámico del turno (va al final; no invalida el cache estable)
    dynamic_parts: list[str] = []
    if ctx.active_skills_summary:
        dynamic_parts.append(f"## Capacidades activas en este turno\n{ctx.active_skills_summary}")
    if ctx.entity_profile:
        dynamic_parts.append(f"## Lo que sabes de este cliente\n{ctx.entity_profile}")
    if ctx.recalled_memories:
        recalled = "\n".join(f"- {m}" for m in ctx.recalled_memories)
        dynamic_parts.append(f"## Hechos relevantes de interacciones pasadas\n{recalled}")
    if ctx.referencias:
        # Ya saneadas por el core (`core/orchestrator/referencias.py`): solo datos.
        referencias = "\n".join(ctx.referencias)
        dynamic_parts.append(f"## Referencias vigentes de esta conversación\n{referencias}")
    # Siempre la última parte: cambia cada minuto y no debe tocar el prefijo cacheado.
    dynamic_parts.append(_current_datetime_part(ctx))
    blocks.append("\n\n".join(dynamic_parts))

    return blocks
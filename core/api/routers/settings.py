"""Configuración del agente (lectura y escritura).

`GET /api/v1/admin/settings` devuelve la configuración del tenant activo;
`PUT .../identity` y `PUT .../brain` la actualizan. Reusa la maquinaria de
versionado del `AgentSpec` (`core.agents.spec`) para no duplicar lógica con el
panel SSR. Requiere la sección `settings`.

Nota de alcance: cubre Identidad (incl. persona) y Cerebro. Las pestañas Memoria,
Habilidades y Avanzado del panel SSR quedan pendientes.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from core.admin.settings_service import (
    read_memory,
    read_skills,
    save_memory,
    save_skills,
    validate_business_ranges,
)
from core.agents.spec import enabled_packs, load_active_spec, save_spec_if_changed
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.config import get_settings
from core.db.models import AppSettings
from core.db.session import get_session
from core.llm.difficulty_router import (
    DIFFICULTY_SCORE_THRESHOLD,
    LONG_MESSAGE_THRESHOLD,
    TURNOS_SIN_RESOLVER_THRESHOLD,
)
from core.llm.prompt_builder import DEFAULT_TIMEZONE
from core.mcp.host import mcp_host
from core.orchestrator.verificador import TOGGLES, normalizar_desactivados

router = APIRouter(prefix="/admin/settings", tags=["admin · configuración"])

# Qué hace el loop al agotar los reintentos del guardrail (ADR-0092).
GuardrailFailAction = Literal["escalate", "neutral"]


# ------------------------------------------------------------------ modelos
class IdentitySettings(BaseModel):
    assistant_name: str
    business_description: str
    brand_voice: str
    brand_voice_warning: bool = False  # True si quedó vacía (#158): el asistente no tiene voz de marca
    tono: str
    debounce_mode: str
    split_style: str = "un_mensaje"
    # Moneda del negocio (HU-UI-01): COP por defecto.
    currency: str = "COP"
    # Zona horaria del negocio (HU-CAL-04, ADR-0091).
    timezone: str = DEFAULT_TIMEZONE
    guardrail_max_retries: int
    guardrail_fail_action: GuardrailFailAction = "escalate"
    disabled_guardrails: list[str]
    available_guardrails: list[str]
    difficulty_long_message_threshold: int
    difficulty_turnos_sin_resolver_threshold: int
    difficulty_score_threshold: int
    escalation_keywords: list[str]
    out_of_hours_message: str
    auto_escalate_after_turns: int
    persona_tone: str
    persona_warmth: int
    persona_emojis: str
    persona_max_length: int
    persona_forbidden: list[str]
    bot_paused: bool
    business_tz: str
    business_ranges: list[dict[str, Any]]


class IdentityUpdate(BaseModel):
    assistant_name: str
    business_description: str = ""
    brand_voice: str = ""
    tono: str = "calido"
    debounce_mode: str = "natural"
    split_style: str = "un_mensaje"
    currency: str = "COP"
    # `None` = no cambiar: un cliente que todavía no conoce el campo no lo pisa.
    timezone: str | None = None
    guardrail_max_retries: int = 1
    guardrail_fail_action: GuardrailFailAction | None = None
    disabled_guardrails: list[str] = []
    difficulty_long_message_threshold: int = LONG_MESSAGE_THRESHOLD
    difficulty_turnos_sin_resolver_threshold: int = TURNOS_SIN_RESOLVER_THRESHOLD
    difficulty_score_threshold: int = DIFFICULTY_SCORE_THRESHOLD
    escalation_keywords: list[str] = []
    out_of_hours_message: str = ""
    auto_escalate_after_turns: int = 0
    persona_tone: str = ""
    persona_warmth: int = 3
    persona_emojis: str = "sparse"
    persona_max_length: int = 500
    persona_forbidden: list[str] = []
    bot_paused: bool = False
    business_tz: str = ""
    business_ranges: list[dict[str, Any]] = []

    @field_validator("timezone")
    @classmethod
    def _zona_horaria_valida(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"zona horaria desconocida: {value}") from exc
        return value


class BrainSettings(BaseModel):
    model_primary: str
    model_fallback: str | None = None
    model_router: str | None = None
    model_temperature: float = 0.3
    policy_max_tool_calls: int = 10
    policy_budget: float = 0.5


class MemorySettings(BaseModel):
    short_term_turns: int
    rag_top_k: int
    rag_min_score: float


class SkillBinding(BaseModel):
    name: str
    plugin_id: str
    description: str | None = None
    enabled: bool = True
    max_calls_per_conversation: int | None = None
    requires_confirmation: bool = False


class SkillsUpdate(BaseModel):
    tools: list[SkillBinding]


class WebChatSettings(BaseModel):
    """Configuración del chat web embebible (HU-CHAN-02). El secreto de Turnstile
    NUNCA se devuelve; solo si está configurado (`turnstile_configured`)."""

    enabled: bool = False
    key: str = ""  # clave pública que va en el fragmento del widget
    allowed_origins: list[str] = []
    turnstile_configured: bool = False
    snippet: str = ""  # fragmento <script> listo para pegar


class WebChatUpdate(BaseModel):
    enabled: bool = False
    allowed_origins: list[str] = []
    turnstile_secret: str | None = None  # None = no tocar; "" = borrar
    rotate_key: bool = False


class SettingsResponse(BaseModel):
    identity: IdentitySettings
    brain: BrainSettings
    memory: MemorySettings
    skills: list[SkillBinding]


# ------------------------------------------------------------------ helpers
async def _load_app_settings(tenant_id) -> AppSettings | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


def _available_guardrails(spec) -> list[str]:
    """Guardrails de los packs habilitados más los interruptores del verificador
    del core (ADR-0119): sin ellos, guardar la configuración borraría lo que el
    negocio había apagado."""
    enabled = enabled_packs(spec)
    return sorted({g.name for g in mcp_host.get_guardrails() if g.plugin_id in enabled} | set(TOGGLES))


def _normalize_origins(origins: list[str]) -> list[str]:
    """Normaliza dominios permitidos: esquema+host, sin barra final, únicos.
    Solo se aceptan orígenes `http(s)://host[:puerto]`."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in origins:
        value = (raw or "").strip().rstrip("/")
        if not value:
            continue
        if not (value.startswith("http://") or value.startswith("https://")):
            continue
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        out.append(value)
    return out


def _web_chat_out(app_settings: AppSettings | None) -> WebChatSettings:
    config = dict((app_settings.web_chat_config if app_settings else {}) or {})
    key = str(config.get("key", "") or "")
    landing = get_settings().landing_base_url.rstrip("/")
    snippet = (
        f'<script src="{landing}/widget.js" data-key="{key}" async></script>'
        if key
        else ""
    )
    return WebChatSettings(
        enabled=bool(config.get("enabled")),
        key=key,
        allowed_origins=list(config.get("allowed_origins", []) or []),
        turnstile_configured=bool(str(config.get("turnstile_secret", "") or "").strip()),
        snippet=snippet,
    )


def _identity_out(app_settings: AppSettings | None, spec, available: list[str]) -> IdentitySettings:
    behavior = dict((app_settings.behavior_config if app_settings else {}) or {})
    escalation = dict((app_settings.escalation_config if app_settings else {}) or {})
    persona = (spec.model_dump(mode="json").get("persona") or {})
    return IdentitySettings(
        assistant_name=(app_settings.assistant_name if app_settings else "") or "",
        business_description=(app_settings.business_description if app_settings else "") or "",
        brand_voice=(app_settings.brand_voice if app_settings else "") or "",
        brand_voice_warning=not bool(((app_settings.brand_voice if app_settings else "") or "").strip()),
        tono=behavior.get("tono", "calido"),
        debounce_mode=behavior.get("debounce_mode", "natural"),
        split_style=behavior.get("split_style", "un_mensaje"),
        currency=(app_settings.currency if app_settings and app_settings.currency else "COP"),
        timezone=(app_settings.timezone if app_settings and app_settings.timezone else DEFAULT_TIMEZONE),
        guardrail_max_retries=int(behavior.get("guardrail_max_retries", 1) or 0),
        guardrail_fail_action="neutral" if behavior.get("guardrail_fail_action") == "neutral" else "escalate",
        disabled_guardrails=normalizar_desactivados(behavior.get("disabled_guardrails", [])),
        available_guardrails=available,
        difficulty_long_message_threshold=int(
            behavior.get("difficulty_long_message_threshold", LONG_MESSAGE_THRESHOLD)
        ),
        difficulty_turnos_sin_resolver_threshold=int(
            behavior.get("difficulty_turnos_sin_resolver_threshold", TURNOS_SIN_RESOLVER_THRESHOLD)
        ),
        difficulty_score_threshold=int(
            behavior.get("difficulty_score_threshold", DIFFICULTY_SCORE_THRESHOLD)
        ),
        escalation_keywords=list(escalation.get("keywords", []) or []),
        out_of_hours_message=escalation.get("out_of_hours_message", "") or "",
        auto_escalate_after_turns=int(escalation.get("auto_escalate_after_turns", 0) or 0),
        persona_tone=persona.get("tone", "") or "",
        persona_warmth=int(persona.get("warmth", 3) or 3),
        persona_emojis=persona.get("emojis", "sparse") or "sparse",
        persona_max_length=int(persona.get("maxLength", 500) or 0),
        persona_forbidden=list(persona.get("forbiddenPhrases", []) or []),
        bot_paused=bool(app_settings.bot_paused) if app_settings else False,
        business_tz=str(((escalation.get("business_hours") or {}).get("tz")) or ""),
        business_ranges=validate_business_ranges(
            (escalation.get("business_hours") or {}).get("ranges") or []
        ),
    )


def _brain_out(spec) -> BrainSettings:
    data = spec.model_dump(mode="json")
    model = data.get("model") or {}
    policies = data.get("policies") or {}
    return BrainSettings(
        model_primary=model.get("primary", "") or "",
        model_fallback=model.get("fallback"),
        model_router=model.get("router"),
        model_temperature=float(model.get("temperature", 0.3) or 0.0),
        policy_max_tool_calls=int(policies.get("maxToolCallsPerTurn", 10) or 10),
        policy_budget=float(policies.get("budgetUsdPerConversation", 0.5) or 0.0),
    )


# ------------------------------------------------------------------ endpoints
@router.get("", response_model=SettingsResponse)
async def get_settings_api(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> SettingsResponse:
    spec = await load_active_spec()
    app_settings = await _load_app_settings(principal.tenant_id)
    return SettingsResponse(
        identity=_identity_out(app_settings, spec, _available_guardrails(spec)),
        brain=_brain_out(spec),
        memory=MemorySettings(**read_memory(spec)),
        skills=[SkillBinding(**item) for item in read_skills(spec)],
    )


@router.put("/identity", response_model=IdentitySettings)
async def save_identity(
    payload: IdentityUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> IdentitySettings:
    spec = await load_active_spec()
    available = _available_guardrails(spec)

    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == principal.tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=principal.tenant_id)
            session.add(app_settings)
        app_settings.assistant_name = payload.assistant_name
        app_settings.business_description = payload.business_description
        app_settings.brand_voice = payload.brand_voice
        # Moneda del negocio (HU-UI-01): ISO-4217 en mayúsculas, default COP.
        app_settings.currency = (payload.currency or "COP").strip().upper()[:3] or "COP"
        if payload.timezone is not None:
            app_settings.timezone = payload.timezone
        behavior = dict(app_settings.behavior_config)
        behavior.update(
            tono=payload.tono,
            debounce_mode=payload.debounce_mode,
            split_style=payload.split_style
            if payload.split_style in ("un_mensaje", "pocos_cortos", "varios_cortos")
            else "un_mensaje",
            # Mismo rango que aplica el loop (`core/orchestrator/loop.py`).
            guardrail_max_retries=max(0, min(5, payload.guardrail_max_retries)),
            disabled_guardrails=sorted(set(available) & set(normalizar_desactivados(payload.disabled_guardrails))),
            difficulty_long_message_threshold=max(
                50, min(5000, payload.difficulty_long_message_threshold)
            ),
            difficulty_turnos_sin_resolver_threshold=max(
                1, min(20, payload.difficulty_turnos_sin_resolver_threshold)
            ),
            difficulty_score_threshold=max(1, min(10, payload.difficulty_score_threshold)),
        )
        if payload.guardrail_fail_action is not None:
            behavior["guardrail_fail_action"] = payload.guardrail_fail_action
        app_settings.behavior_config = behavior
        ranges = validate_business_ranges(payload.business_ranges)
        # Sin zona propia, el horario de atención usa la del negocio (ADR-0091).
        business_tz = payload.business_tz.strip() or app_settings.timezone or DEFAULT_TIMEZONE
        business_hours = {"tz": business_tz, "ranges": ranges} if ranges else None
        app_settings.escalation_config = {
            **dict(app_settings.escalation_config or {}),
            "keywords": [k.strip() for k in payload.escalation_keywords if k.strip()],
            "out_of_hours_message": payload.out_of_hours_message.strip(),
            "auto_escalate_after_turns": max(0, payload.auto_escalate_after_turns),
            "business_hours": business_hours,
        }
        app_settings.bot_paused = payload.bot_paused

    persona_data = spec.model_dump(mode="json")
    persona_data["persona"].update(
        {
            "tone": payload.persona_tone or persona_data["persona"].get("tone", ""),
            "warmth": max(0, min(5, payload.persona_warmth)),
            "emojis": payload.persona_emojis or persona_data["persona"].get("emojis", "sparse"),
            "maxLength": max(0, payload.persona_max_length),
            "forbiddenPhrases": [p.strip() for p in payload.persona_forbidden if p.strip()],
        }
    )
    await save_spec_if_changed(spec, persona_data, principal.tenant_id)

    refreshed = await _load_app_settings(principal.tenant_id)
    return _identity_out(refreshed, spec, available)


@router.put("/brain", response_model=BrainSettings)
async def save_brain(
    payload: BrainSettings,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> BrainSettings:
    spec = await load_active_spec()
    data: dict[str, Any] = spec.model_dump(mode="json")
    if payload.model_primary:
        data["model"]["primary"] = payload.model_primary.strip()
    data["model"]["fallback"] = (payload.model_fallback or "").strip() or None
    data["model"]["router"] = (payload.model_router or "").strip() or None
    data["model"]["temperature"] = max(0.0, min(2.0, payload.model_temperature))
    data["policies"]["maxToolCallsPerTurn"] = max(1, min(50, payload.policy_max_tool_calls))
    data["policies"]["budgetUsdPerConversation"] = max(0.0, payload.policy_budget)
    await save_spec_if_changed(spec, data, principal.tenant_id)

    updated = await load_active_spec()
    return _brain_out(updated)


@router.get("/web-chat", response_model=WebChatSettings)
async def get_web_chat_settings(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> WebChatSettings:
    """Config del chat web embebible (HU-CHAN-02). Nunca devuelve el secreto."""
    app_settings = await _load_app_settings(principal.tenant_id)
    return _web_chat_out(app_settings)


@router.put("/web-chat", response_model=WebChatSettings)
async def save_web_chat_settings(
    payload: WebChatUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> WebChatSettings:
    """Habilita/deshabilita el chat web, edita dominios permitidos, rota la clave
    o cambia el secreto de Turnstile. Todo se guarda en
    `app_settings.web_chat_config` (JSONB, migración 0047)."""
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == principal.tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=principal.tenant_id)
            session.add(app_settings)

        config = dict(app_settings.web_chat_config or {})
        if not config.get("key"):
            config["key"] = secrets.token_urlsafe(24)
        elif payload.rotate_key:
            config["key"] = secrets.token_urlsafe(24)
        config["enabled"] = bool(payload.enabled)
        config["allowed_origins"] = _normalize_origins(payload.allowed_origins)
        if payload.turnstile_secret is not None:
            config["turnstile_secret"] = payload.turnstile_secret.strip()
        app_settings.web_chat_config = config

    refreshed = await _load_app_settings(principal.tenant_id)
    return _web_chat_out(refreshed)


@router.put("/memory", response_model=MemorySettings)
async def save_memory_settings(
    payload: MemorySettings,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> MemorySettings:
    """Memoria del agente: turnos de contexto y recall RAG."""
    await save_memory(
        principal.tenant_id,
        short_term_turns=payload.short_term_turns,
        rag_top_k=payload.rag_top_k,
        rag_min_score=payload.rag_min_score,
    )
    spec = await load_active_spec()
    return MemorySettings(**read_memory(spec))


@router.put("/skills", response_model=list[SkillBinding])
async def save_skills_settings(
    payload: SkillsUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> list[SkillBinding]:
    """On/off y límites por herramienta (solo tools de packs habilitados)."""
    await save_skills(
        principal.tenant_id,
        tools=[tool.model_dump() for tool in payload.tools],
    )
    spec = await load_active_spec()
    return [SkillBinding(**item) for item in read_skills(spec)]


# ------------------------------------------------------------------ avanzado
class SpecVersion(BaseModel):
    id: str
    version: str
    is_active: bool
    created_at: datetime | None = None


class VersionsResponse(BaseModel):
    versions: list[SpecVersion]


class ActivateVersionRequest(BaseModel):
    record_id: str


class LlmSettings(BaseModel):
    llm_provider: str
    llm_model: str
    has_api_key: bool


class LlmUpdate(BaseModel):
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = ""


class GuardrailsSettings(BaseModel):
    guardrails_text: str
    # Líneas del DSL que no se pudieron aplicar (DEF-01): se guardan las válidas
    # pero el panel debe avisar de estas.
    ignored: list[str] = []


class GuardrailsUpdate(BaseModel):
    guardrails_text: str


class FlowSection(BaseModel):
    label: str
    purpose: str | None = None
    tools: list[str] = []


class FlowResponse(BaseModel):
    source: str
    sections: list[FlowSection]
    unknown_tools: list[str] = []


class EvalRunItem(BaseModel):
    id: str
    status: str
    score: float | None = None
    created_at: datetime | None = None


class AdvancedResponse(BaseModel):
    versions: list[SpecVersion]
    llm: LlmSettings
    guardrails_text: str
    flow: FlowResponse
    eval_runs: list[EvalRunItem]
    # AgentSpec activo tal cual: alimenta el editor crudo del panel.
    spec: dict[str, Any] = {}


@router.get("/advanced", response_model=AdvancedResponse)
async def get_advanced(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> AdvancedResponse:
    """Versiones del AgentSpec, LLM, guardrails, flow de atención y evals."""
    from core.admin.advanced_service import (
        list_spec_versions,
        read_custom_guardrails,
        read_flow,
        read_llm,
        recent_eval_runs,
    )

    spec = await load_active_spec()
    spec = await load_active_spec()
    return AdvancedResponse(
        spec=spec.model_dump(mode="json"),
        versions=[SpecVersion(**v) for v in await list_spec_versions(principal.tenant_id)],
        llm=LlmSettings(**await read_llm(principal.tenant_id)),
        guardrails_text=read_custom_guardrails(spec),
        flow=FlowResponse(**await read_flow(spec)),
        eval_runs=[EvalRunItem(**r) for r in await recent_eval_runs(principal.tenant_id)],
    )


@router.post("/advanced/versions/{version_id}/activate", response_model=SpecVersion)
async def activate_spec_version_endpoint(
    version_id: str,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> SpecVersion:
    """Activa (o revierte a) una versión concreta del AgentSpec (ADR-0051)."""
    from core.admin.advanced_service import activate_version, list_spec_versions

    _version, error = await activate_version(principal.tenant_id, version_id)
    if error is not None:
        raise ApiError.bad_request(error, "activate_failed")

    versions = await list_spec_versions(principal.tenant_id)
    active = next((v for v in versions if v["is_active"]), None)
    if active is None:
        raise ApiError(502, "activate_failed", "No quedó una versión activa.")
    return SpecVersion(**active)


@router.put("/advanced/llm", response_model=LlmSettings)
async def save_llm_settings(
    payload: LlmUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> LlmSettings:
    """Proveedor y modelo LLM del negocio (BYOK); la llave vacía conserva la actual."""
    from core.admin.advanced_service import read_llm, save_llm

    await save_llm(
        principal.tenant_id,
        llm_provider=payload.llm_provider,
        llm_model=payload.llm_model,
        llm_api_key=payload.llm_api_key,
    )
    return LlmSettings(**await read_llm(principal.tenant_id))


@router.put("/advanced/guardrails", response_model=GuardrailsSettings)
async def save_guardrails_settings(
    payload: GuardrailsUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> GuardrailsSettings:
    """Guardrails personalizados del tenant (DSL seguro, una regla por línea)."""
    from core.admin.advanced_service import (
        read_custom_guardrails,
        save_custom_guardrails_report,
    )

    error, ignoradas = await save_custom_guardrails_report(
        principal.tenant_id, payload.guardrails_text
    )
    if error is not None:
        raise ApiError.bad_request(error, "invalid_guardrails")
    spec = await load_active_spec()
    return GuardrailsSettings(
        guardrails_text=read_custom_guardrails(spec), ignored=ignoradas
    )


class SpecPayload(BaseModel):
    spec: dict[str, Any]


@router.get("/advanced/spec", response_model=SpecPayload)
async def get_raw_spec(
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> SpecPayload:
    """AgentSpec activo tal cual (editor crudo de Configuración → Avanzado)."""
    spec = await load_active_spec()
    return SpecPayload(spec=spec.model_dump(mode="json"))


@router.put("/advanced/spec", response_model=SpecPayload)
async def save_raw_spec(
    payload: SpecPayload,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> SpecPayload:
    """Valida el JSON contra el contrato del AgentSpec y guarda si cambió.

    `save_spec_if_changed` solo crea versión cuando el contenido difiere, así que
    guardar el mismo spec es un no-op (no ensucia el historial).
    """
    from agentcore_contracts import AgentSpec
    from pydantic import ValidationError

    try:
        nuevo = AgentSpec.model_validate(payload.spec)
    except ValidationError as exc:
        raise ApiError.bad_request(f"Spec inválido: {str(exc)[:240]}", "invalid_spec") from exc
    spec = await load_active_spec()
    await save_spec_if_changed(spec, nuevo.model_dump(mode="json"), principal.tenant_id)
    return SpecPayload(spec=nuevo.model_dump(mode="json"))


# ---------------------------------------------------------------- plantillas por evento
EVENTO_LABEL = {
    "subscription.charge_created": "Cargo de suscripción",
    "subscription.charge_reminder": "Recordatorio de pago",
    "subscription.paused": "Suscripción pausada por mora",
    "subscription.trial_ending": "Fin de la prueba gratis",
    "appointment.confirmed": "Cita agendada desde el panel",
    "appointment.reminder": "Recordatorio de cita",
    "appointment.no_show": "Cita perdida: oferta de reagendar",
}

class EventTemplateEntry(BaseModel):
    """Evento → plantilla aprobada (HU-ARCH-13, ADR-0108)."""

    template: str = ""
    language: str = "es"
    # Lista (variables posicionales, en orden) o {placeholder: ruta} (con nombre).
    variables: list[str] | dict[str, str] = []
    fallback_text: str = ""


class EventCatalogItem(BaseModel):
    event: str
    variables: dict[str, str]  # ruta permitida → descripción


class TenantTemplateItem(BaseModel):
    """Plantilla APROBADA de alguna WABA del negocio, con los placeholders del cuerpo."""

    name: str
    language: str
    category: str | None
    body: str
    variables: list[str]
    named: bool


class MessageTemplatesSettings(BaseModel):
    events: list[EventCatalogItem]
    mapping: dict[str, EventTemplateEntry]
    templates: list[TenantTemplateItem] = []


class MessageTemplatesUpdate(BaseModel):
    mapping: dict[str, EventTemplateEntry]


class MissingTemplateItem(BaseModel):
    conversation_id: str
    detail: str
    created_at: datetime


class MissingTemplates(BaseModel):
    total_7d: int
    recent: list[MissingTemplateItem]


async def _message_templates_out(tenant_id) -> MessageTemplatesSettings:
    from core.db.models import MessageTemplate
    from core.gateway.event_notices import (
        EVENTOS,
        render_body,
        template_has_non_body_variables,
        template_placeholders,
    )

    app_settings = await _load_app_settings(tenant_id)
    raw = dict((app_settings.message_templates_map if app_settings else None) or {})
    async with get_session() as session:
        plantillas = (
            await session.execute(
                select(MessageTemplate)
                .where(MessageTemplate.tenant_id == tenant_id, MessageTemplate.status == "APPROVED")
                .order_by(MessageTemplate.name, MessageTemplate.language)
            )
        ).scalars().all()
    vistas: set[tuple[str, str]] = set()
    templates: list[TenantTemplateItem] = []
    for t in plantillas:
        # La misma plantilla puede estar en varias WABA del negocio: una sola entrada.
        if (t.name, t.language) in vistas or template_has_non_body_variables(t.components):
            continue
        vistas.add((t.name, t.language))
        templates.append(
            TenantTemplateItem(
                name=t.name,
                language=t.language,
                category=t.category,
                body=render_body(t, {}),
                variables=template_placeholders(t.components),
                named=(t.parameter_format or "").upper() == "NAMED",
            )
        )
    return MessageTemplatesSettings(
        events=[EventCatalogItem(event=k, variables=v) for k, v in EVENTOS.items()],
        mapping={k: EventTemplateEntry.model_validate(v) for k, v in raw.items() if k in EVENTOS},
        templates=templates,
    )


@router.get("/message-templates", response_model=MessageTemplatesSettings)
async def get_message_templates(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> MessageTemplatesSettings:
    """Catálogo de eventos (con sus variables permitidas) y el mapeo del negocio."""
    return await _message_templates_out(principal.tenant_id)


@router.put("/message-templates", response_model=MessageTemplatesSettings)
async def save_message_templates(
    payload: MessageTemplatesUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> MessageTemplatesSettings:
    """Guarda el mapeo evento → plantilla. Valida que el evento exista, que las
    rutas estén permitidas y que la plantilla exista en alguna WABA del negocio
    con la misma cantidad de variables en el cuerpo."""
    from core.db.models import MessageTemplate
    from core.gateway.event_notices import (
        EVENTOS,
        template_has_non_body_variables,
        template_placeholders,
    )

    errores: dict[str, str] = {}
    limpio: dict[str, Any] = {}
    for evento, entrada in payload.mapping.items():
        if evento not in EVENTOS:
            errores[evento] = "Evento desconocido."
            continue
        nombre = entrada.template.strip()
        idioma = entrada.language.strip() or "es"
        fallback = entrada.fallback_text.strip()[:1000]
        if not nombre:
            if fallback:
                limpio[evento] = {"template": "", "language": idioma, "variables": [], "fallback_text": fallback}
            continue
        rutas = list(entrada.variables.values()) if isinstance(entrada.variables, dict) else list(entrada.variables)
        no_permitidas = [r for r in rutas if r not in EVENTOS[evento]]
        if no_permitidas:
            errores[evento] = f"Variables no permitidas: {', '.join(no_permitidas)}."
            continue
        async with get_session() as session:
            plantilla = (
                await session.execute(
                    select(MessageTemplate).where(
                        MessageTemplate.tenant_id == principal.tenant_id,
                        MessageTemplate.name == nombre,
                        MessageTemplate.language == idioma,
                    )
                )
            ).scalars().first()
        if plantilla is None:
            errores[evento] = f"No existe la plantilla {nombre} ({idioma}) en tus conexiones."
            continue
        if template_has_non_body_variables(plantilla.components):
            errores[evento] = "La plantilla tiene variables fuera del cuerpo (cabecera o botón): no se admite todavía."
            continue
        placeholders = template_placeholders(plantilla.components)
        claves = set(entrada.variables) if isinstance(entrada.variables, dict) else None
        if (claves is not None and claves != set(placeholders)) or (
            claves is None and len(rutas) != len(placeholders)
        ):
            errores[evento] = f"La plantilla tiene {len(placeholders)} variables y mapeaste {len(rutas)}."
            continue
        limpio[evento] = {
            "template": nombre,
            "language": idioma,
            "variables": entrada.variables,
            "fallback_text": fallback,
        }
    if errores:
        detalle = " ".join(f"{EVENTO_LABEL.get(k, k)}: {v}" for k, v in errores.items())
        raise ApiError.bad_request(
            f"Revisa el mapeo de plantillas. {detalle}", "message_templates_invalid", fields=errores
        )

    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == principal.tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=principal.tenant_id)
            session.add(app_settings)
        app_settings.message_templates_map = limpio
    return await _message_templates_out(principal.tenant_id)


@router.get("/message-templates/missing", response_model=MissingTemplates)
async def get_missing_templates(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> MissingTemplates:
    """Avisos que no salieron por falta de plantilla en los últimos 7 días (alerta del panel)."""
    from datetime import UTC, timedelta

    from sqlalchemy import func

    from core.db.models import Message
    from core.gateway.event_notices import SIN_PLANTILLA

    desde = datetime.now(UTC) - timedelta(days=7)
    filtro = (
        Message.tenant_id == principal.tenant_id,
        Message.error_reason == SIN_PLANTILLA,
        Message.created_at >= desde,
        Message.deleted_at.is_(None),
    )
    async with get_session() as session:
        total = (await session.execute(select(func.count()).select_from(Message).where(*filtro))).scalar_one()
        filas = (
            await session.execute(
                select(Message).where(*filtro).order_by(Message.created_at.desc()).limit(10)
            )
        ).scalars().all()
    return MissingTemplates(
        total_7d=int(total),
        recent=[
            MissingTemplateItem(conversation_id=str(m.conversation_id), detail=m.content, created_at=m.created_at)
            for m in filas
        ],
    )


# ------------------------------------------------- proveedor de suscripciones (HU-SUB-09)
class ProveedorSuscripcion(BaseModel):
    id: str
    label: str


class SubscriptionProviderSettings(BaseModel):
    estado: Literal["ok", "sin_proveedor", "ambiguo", "invalido"]
    configurado: str | None = None
    efectivo: str | None = None
    habilitados: list[ProveedorSuscripcion] = []
    mensaje: str = ""


class SubscriptionProviderUpdate(BaseModel):
    provider: str | None = None


async def _subscription_provider_out(tenant_id) -> SubscriptionProviderSettings:  # type: ignore[no-untyped-def]
    from core.plugin_manager import plugin_manager
    from core.subscriptions import estado_proveedor

    estado = await estado_proveedor(tenant_id)

    def _label(pid: str) -> str:
        modulo = plugin_manager.get(pid)
        return (modulo.display_name or modulo.name) if modulo else pid

    return SubscriptionProviderSettings(
        estado=estado["estado"],  # type: ignore[arg-type]
        configurado=estado["configurado"],
        efectivo=estado["efectivo"],
        habilitados=[ProveedorSuscripcion(id=pid, label=_label(pid)) for pid in estado["habilitados"]],
        mensaje=estado["mensaje"],
    )


@router.get("/subscription-provider", response_model=SubscriptionProviderSettings)
async def get_subscription_provider(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> SubscriptionProviderSettings:
    """Qué habilidad cobra los planes del negocio y, si no hay una clara, por qué."""
    return await _subscription_provider_out(principal.tenant_id)


@router.put("/subscription-provider", response_model=SubscriptionProviderSettings)
async def save_subscription_provider(
    payload: SubscriptionProviderUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> SubscriptionProviderSettings:
    """Elige el proveedor entre los habilitados (o lo borra con `null`).

    Las activaciones de planes que quedaron pendientes por la configuración las
    reintenta el pack del pedido en su próximo ciclo.
    """
    from core.subscriptions import guardar_proveedor, subscription_registry

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise ApiError.bad_request("Elige un negocio primero.", "sin_negocio")
    provider = (payload.provider or "").strip() or None
    if provider is not None and provider not in await subscription_registry.enabled_for_tenant(tenant_id):
        raise ApiError.bad_request(
            "Ese proveedor de suscripciones no está habilitado para este negocio.", "proveedor_no_habilitado"
        )
    await guardar_proveedor(tenant_id, provider)
    return await _subscription_provider_out(tenant_id)

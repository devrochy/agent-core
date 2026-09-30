"""Configuración del agente (lectura y escritura).

`GET /api/v1/admin/settings` devuelve la configuración del tenant activo;
`PUT .../identity` y `PUT .../brain` la actualizan. Reusa la maquinaria de
versionado del `AgentSpec` (`core.agents.spec`) para no duplicar lógica con el
panel SSR. Requiere la sección `settings`.

Nota de alcance: cubre Identidad (incl. persona) y Cerebro. Las pestañas Memoria,
Habilidades y Avanzado del panel SSR quedan pendientes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
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
from core.db.models import AppSettings
from core.db.session import get_session
from core.llm.difficulty_router import (
    DIFFICULTY_SCORE_THRESHOLD,
    LONG_MESSAGE_THRESHOLD,
    TURNOS_SIN_RESOLVER_THRESHOLD,
)
from core.mcp.host import mcp_host

router = APIRouter(prefix="/admin/settings", tags=["admin · configuración"])


# ------------------------------------------------------------------ modelos
class IdentitySettings(BaseModel):
    assistant_name: str
    business_description: str
    brand_voice: str
    tono: str
    debounce_mode: str
    guardrail_max_retries: int
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
    guardrail_max_retries: int = 1
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
    enabled = enabled_packs(spec)
    return sorted(g.name for g in mcp_host.get_guardrails() if g.plugin_id in enabled)


def _identity_out(app_settings: AppSettings | None, spec, available: list[str]) -> IdentitySettings:
    behavior = dict((app_settings.behavior_config if app_settings else {}) or {})
    escalation = dict((app_settings.escalation_config if app_settings else {}) or {})
    persona = (spec.model_dump(mode="json").get("persona") or {})
    return IdentitySettings(
        assistant_name=(app_settings.assistant_name if app_settings else "") or "",
        business_description=(app_settings.business_description if app_settings else "") or "",
        brand_voice=(app_settings.brand_voice if app_settings else "") or "",
        tono=behavior.get("tono", "calido"),
        debounce_mode=behavior.get("debounce_mode", "natural"),
        guardrail_max_retries=int(behavior.get("guardrail_max_retries", 1) or 0),
        disabled_guardrails=list(behavior.get("disabled_guardrails", []) or []),
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
        behavior = dict(app_settings.behavior_config)
        behavior.update(
            tono=payload.tono,
            debounce_mode=payload.debounce_mode,
            guardrail_max_retries=max(0, payload.guardrail_max_retries),
            disabled_guardrails=sorted(set(available) & set(payload.disabled_guardrails)),
            difficulty_long_message_threshold=max(
                50, min(5000, payload.difficulty_long_message_threshold)
            ),
            difficulty_turnos_sin_resolver_threshold=max(
                1, min(20, payload.difficulty_turnos_sin_resolver_threshold)
            ),
            difficulty_score_threshold=max(1, min(10, payload.difficulty_score_threshold)),
        )
        app_settings.behavior_config = behavior
        ranges = validate_business_ranges(payload.business_ranges)
        business_hours = (
            {"tz": payload.business_tz.strip() or "UTC", "ranges": ranges} if ranges else None
        )
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

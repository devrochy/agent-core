"""Agentic Loop: el motor del asistente.

Por turno (hasta MAX_AGENT_ITERATIONS vueltas):
  1. Recall de memoria (3 capas) + selección de skills + system blocks.
  2. Difficulty routing (deepseek-chat vs deepseek-reasoner).
  3. Llamada al LLM. Registro de tokens/costo/latencia en usage_ledger (ADR-0008).
  4. tool_use  -> ejecutar tools vía MCPHost (PluginError => auto-disable, el loop sigue).
  5. end_turn  -> guardrails -> ok? SafeExtras + respuesta final.
                            -> falla? 1 reintento correctivo; si reincide, escalar.
  6. refusal   -> escalar.

El LLM propone, las tools deciden (contra la BD), el core arbitra.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from core.agents.flow import render_flow, resolve_flow
from core.agents.responses import load_response
from core.agents.spec import enabled_packs, load_active_spec
from core.config import get_settings
from core.db.models import AppSettings, AuditLog, Conversation, Customer, Message, UsageLedger
from core.db.session import get_session
from core.llm import resolve_provider
from core.llm.difficulty_router import (
    DIFFICULTY_SCORE_THRESHOLD,
    LONG_MESSAGE_THRESHOLD,
    TURNOS_SIN_RESOLVER_THRESHOLD,
    route_model,
)
from core.llm.prompt_builder import PromptContext, build_system_blocks
from core.llm.types import LLMMessage, LLMToolCall
from core.mcp.host import mcp_host, new_tool_context
from core.mcp.translator import skills_summary, to_llm_schemas
from core.mcp.types import GuardrailResult
from core.memory import entity as entity_memory
from core.memory import vector as vector_memory
from core.memory import working as working_memory
from core.observability.tracing import get_tracer, span_attributes
from core.orchestrator.escalation import ESCALATION_REPLY, escalate
from core.orchestrator.guardrails import MAX_GUARDRAIL_RETRIES, run_guardrails
from core.orchestrator.safe_renderer import (
    TOOL_NAME_KEY,
    apply_safe_extras,
    extract_safe_extras,
    strip_reserved_keys,
)
from core.orchestrator.skill_router import select_relevant_skills
from core.shared.audit import record_audit
from core.shared.crypto import get_secret_box
from core.shared.exceptions import LLMProviderError, PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.tools.core_skill import conversation_spent

log = get_logger(__name__)

FALLBACK_REPLY = (
    "Disculpa, estoy teniendo un inconveniente técnico. "
    "Ya avisé a nuestro equipo para que te atienda una persona."
)

# Se agotó una cuota del período (mensajes o costo LLM). El texto real vive en
# core/playbooks/respuestas.md (`## quota_exhausted`); esto es el fallback
# determinístico si el playbook no está disponible (mismo patrón que FALLBACK_REPLY).
QUOTA_EXHAUSTED_REPLY = (
    "Por ahora no puedo seguir atendiendo esta conversación. "
    "Un asesor humano puede continuar la atención."
)

# WhatsApp y canales similares no interpretan Markdown: un cliente ve
# "**Pollo Broaster**" literal, asteriscos incluidos, no negrita. La regla ya
# está en core/playbooks/reglas-operacion.md, pero confiar solo en que el LLM
# la siga siempre no es suficiente (mismo criterio que los guardrails de esta
# sesión: lo que se puede garantizar en código, no se deja solo al prompt) --
# esto es un strip determinístico de última línea, no un reemplazo del
# playbook.
_MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")


def strip_markdown(text: str) -> str:
    text = _MARKDOWN_BOLD.sub(r"\1", text)
    return _MARKDOWN_ITALIC.sub(r"\1", text)


@dataclass
class TurnResult:
    text: str
    image_url: str | None = None
    escalated: bool = False


@dataclass
class _TurnUsage:
    """Acumula el uso LLM de UN turno para el usage_ledger (ADR-0008)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    model: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    escalated: bool = False

    def add(self, provider, response) -> None:  # type: ignore[no-untyped-def]
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.cached_tokens += getattr(response.usage, "cache_read_tokens", 0)
        self.cached_tokens += getattr(response.usage, "cache_write_tokens", 0)
        self.latency_ms += response.latency_ms
        self.cost_usd += _response_cost(provider, response)
        self.model = response.model or self.model


async def _write_usage_ledger(
    conversation_id: uuid.UUID,
    spec,  # type: ignore[no-untyped-def]
    usage: _TurnUsage,
    span=None,  # type: ignore[no-untyped-def]
) -> None:
    """Una fila append-only por turno. Best-effort: nunca interrumpe la respuesta."""
    try:
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=get_current_tenant_id(),
                    conversation_id=conversation_id,
                    agent_spec_version=spec.version,
                    model=usage.model or spec.model.primary,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    latency_ms=usage.latency_ms,
                    cost_usd=round(usage.cost_usd, 6),
                    tool_calls=usage.tool_calls or None,
                    result={"escalated": usage.escalated},
                )
            )
    except Exception:
        log.warning("usage_ledger_write_failed")
    finally:
        if span is not None:
            span_attributes(
                span,
                cost_usd=usage.cost_usd,
                outcome="escalated" if usage.escalated else "ok",
                tool_calls=len(usage.tool_calls),
            )
            span.end()


async def process_conversation(conversation_id: uuid.UUID, latest_message: str) -> TurnResult | None:
    settings = get_settings()

    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        customer = await session.get(Customer, conversation.customer_id)
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == get_current_tenant_id(),
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    if customer is None or conversation.bot_paused or conversation.status == "escalated":
        return None

    # ---------------- 0. AgentSpec del tenant (Sprint 2, ADR-0014)
    spec = await load_active_spec()

    # Cuota de turno (ADR-0045): si el período agotó `mensajes` o
    # `costo_llm_usd`, se corta el turno antes de gastar LLM. El bloqueo por
    # clientes/conversaciones/audio/OCR ocurre en el consumer/webhook (acción).
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        from core.billing.enforcement import turn_quota_metric

        blocked = await turn_quota_metric(tenant_id)
        if blocked is not None:
            log.warning(
                "turn_quota_exceeded",
                tenant_id=str(tenant_id),
                metric=blocked,
                conversation_id=str(conversation_id),
            )
            closure = await load_response(spec, "quota_exhausted") or QUOTA_EXHAUSTED_REPLY
            return TurnResult(text=closure)

    # Presupuesto agotado (ADR-0015): no se gasta más LLM en esta conversación
    # hasta que un admin suba el presupuesto o lo resetee.
    if conversation.state.get("budget_exhausted"):
        return None

    # ---------------- 1. Memoria y contexto
    history = await _load_history(conversation_id)
    recent_context = " ".join(m.content for m in history[-6:] if m.content)
    history_tool_results = await _historical_tool_results(conversation_id)

    profile = await entity_memory.get_profile(customer.id)
    rag = spec.memory.rag
    recalled = await vector_memory.recall(
        latest_message,
        customer_id=customer.id,
        conversation_id=conversation_id,
        top_k=rag.topK if rag else 3,
        # `minScore` es similitud (1.0 = idéntico); `recall` usa distancia coseno,
        # así que se invierte (ADR-0050).
        max_distance=(1.0 - rag.minScore) if rag else 0.6,
    )

    from core.plugin_manager import plugin_manager

    available_skills = mcp_host.list_skills()
    # Enablement por tenant (ADR-0019): solo skills de los packs del AgentSpec.
    enabled = enabled_packs(spec)
    # Salud por tenant (ADR-0024): un plugin roto para ESTE tenant (por un
    # PluginError de runtime) no debe ofrecerse, aunque siga registrado en
    # mcp_host para los demás tenants. OJO: `enabled` (arriba) se reusa más
    # abajo para filtrar guardrails -- ese filtro NO debe tocarse acá, los
    # guardrails de un plugin con salud rota siguen corriendo a propósito.
    disabled_for_tenant = await plugin_manager.disabled_plugins_for_tenant(get_current_tenant_id())
    available_skills = [
        s for s in available_skills if s.plugin_id in enabled and s.plugin_id not in disabled_for_tenant
    ]
    # Candidatas: TODAS las tools de los packs habilitados (sin router por turno)
    # más las declarativas del tenant (ADR-0052). El flujo (ADR-0054) decide el
    # subconjunto y el orden; sin flujo, se cae al router por keywords de siempre.
    candidate_tools = mcp_host.get_skill_tools([s.name for s in available_skills])
    from core.mcp.declarative import tools_for_spec

    candidate_tools = [*candidate_tools, *tools_for_spec(spec)]

    # Prerequisito operativo (ADR-0072): una tool que necesita una conexión activa
    # (p. ej. google_calendar) no se ofrece si el tenant no la tiene conectada. Se
    # aplica ACÁ y en `_tool_denial` (revocación a mitad de turno).
    from core.gateway.connections import active_connection_type_ids

    connected_types = await active_connection_type_ids(get_current_tenant_id())
    candidate_tools = [t for t in candidate_tools if await _connections_ok(t, connected_types)]

    flow = await resolve_flow(
        spec, available={t.name for t in candidate_tools}
    )
    if flow is not None:
        by_name = {t.name: t for t in candidate_tools}
        active_tools = [by_name[name] for name in flow.tool_names if name in by_name]
        flow_summary = render_flow(flow)
    else:
        selected_names = select_relevant_skills(latest_message, recent_context, available_skills)
        active_tools = mcp_host.get_skill_tools(selected_names)
        active_tools = [*active_tools, *tools_for_spec(spec)]
        flow_summary = ""

    active_tools = [t for t in active_tools if await _connections_ok(t, connected_types)]

    prompt_ctx = PromptContext(
        assistant_name=app_settings.assistant_name if app_settings else settings.app_name,
        business_description=app_settings.business_description if app_settings else "",
        language=app_settings.language if app_settings else "es",
        tone=spec.persona.tone,
        warmth=spec.persona.warmth,
        emojis=spec.persona.emojis,
        max_length=spec.persona.maxLength,
        brand_voice=app_settings.brand_voice if app_settings else "",
        flow_summary=flow_summary,
        active_skills_summary=skills_summary(active_tools),
        recalled_memories=recalled,
        entity_profile=entity_memory.profile_to_prompt(profile),
    )
    system_blocks = await build_system_blocks(spec, prompt_ctx)

    # ---------------- 2. Proveedor y routing
    # `behavior` se adelanta acá (antes vivía después del routing) porque
    # ahora también resuelve los umbrales del difficulty router por tenant
    # -- se reusa el mismo dict más abajo para guardrail_max_retries/
    # disabled_guardrails, sin recalcularlo dos veces.
    behavior = dict(app_settings.behavior_config) if app_settings else {}
    # Clamps (no solo en el form del admin, auditor-multitenant 2026-09-12):
    # behavior_config es JSONB libre, puede llegar por otra vía (script,
    # migración, endpoint futuro) sin pasar por la validación del form --
    # incluido un `None` explícito guardado (una key presente pero vacía,
    # distinta de "sin configurar"), que `_clamped_int` trata igual que si
    # faltara del todo.
    def _clamped_int(key: str, default: int, lo: int, hi: int) -> int:
        raw = behavior.get(key)
        return max(lo, min(hi, int(raw if raw is not None else default)))

    difficulty_long_message_threshold = _clamped_int(
        "difficulty_long_message_threshold", LONG_MESSAGE_THRESHOLD, 50, 5000
    )
    difficulty_turnos_sin_resolver_threshold = _clamped_int(
        "difficulty_turnos_sin_resolver_threshold", TURNOS_SIN_RESOLVER_THRESHOLD, 1, 20
    )
    difficulty_score_threshold = _clamped_int(
        "difficulty_score_threshold", DIFFICULTY_SCORE_THRESHOLD, 1, 10
    )

    turnos_sin_resolver = int(conversation.state.get("turnos_sin_resolver", 0))
    routing = route_model(
        message=latest_message,
        turnos_sin_resolver=turnos_sin_resolver,
        default_model=_resolved_model(app_settings, spec, settings.llm_model),
        # v2.0.0 (ADR-0050): el modelo de razonamiento es por tenant
        # (`spec.model.router`); cae al global del .env si no está seteado.
        reasoning_model=spec.model.router or settings.llm_model_reasoning,
        routing_enabled=settings.difficulty_routing,
        requires_tools=bool(active_tools),
        long_message_threshold=difficulty_long_message_threshold,
        turnos_sin_resolver_threshold=difficulty_turnos_sin_resolver_threshold,
        score_threshold=difficulty_score_threshold,
    )
    provider = _resolved_provider(app_settings, routing.model)
    tool_schemas = to_llm_schemas(active_tools) if routing.allow_tools else None
    # v2.0.0 (ADR-0050): temperatura del tenant (clampeada -- el raw_json de
    # /admin/spec puede traer cualquier float).
    temperature = max(0.0, min(2.0, spec.model.temperature))

    # Reintentos de guardrail antes de escalar de verdad (pedido explícito del
    # usuario: "intentos fallidos" configurable por tenant, mismo patrón exacto
    # que debounce_mode -- JSONB libre en AppSettings, sin tocar el contrato
    # AgentSpec congelado). Fallback al default global si el tenant no lo seteó.
    # `_clamped_int` (no solo `.get(key, default)`): hallazgo de
    # `auditor-multitenant` sobre el diff de los umbrales de dificultad --
    # un `None` explícito en `behavior_config` para esta key tumbaría el
    # turno con `TypeError` sin este mismo cuidado.
    guardrail_max_retries = _clamped_int("guardrail_max_retries", MAX_GUARDRAIL_RETRIES, 0, 5)
    # Guardrail individual on/off por tenant (mismo patrón, ver `AppSettings.
    # behavior_config`): apagar un guardrail puntual (ej. `crm_amount_check`)
    # sin apagar el pack entero.
    disabled_guardrails = set(behavior.get("disabled_guardrails", []))

    # ---------------- 3. Agentic loop
    messages = list(history)
    # _load_history puede devolver turnos cacheados en working memory (Redis)
    # que no incluyen el mensaje que acaba de llegar (append_turn solo escribe
    # ahí al FINAL del turno, en _finish_turn) — sin esto el LLM respondería
    # sin ver el mensaje actual del cliente en cualquier turno posterior al
    # primero de la conversación.
    if not messages or messages[-1].role != "user" or messages[-1].content != latest_message:
        messages.append(LLMMessage(role="user", content=latest_message))
    all_tool_results: list[dict[str, Any]] = []
    guardrail_retries = 0
    turn_cost: float = 0.0
    budget = spec.policies.budgetUsdPerConversation
    usage = _TurnUsage()

    # Span del turno (OTel): atributos para el panel/trazas.
    tracer = get_tracer()
    turn_span = tracer.start_span("turn")
    span_attributes(
        turn_span,
        tenant_id=str(get_current_tenant_id()),
        conversation_id=str(conversation_id),
        agent_spec_version=spec.version,
    )

    max_iterations = max(1, min(50, spec.policies.maxToolCallsPerTurn))
    for _iteration in range(max_iterations):
        # Presupuesto (ADR-0015): se mide ANTES de llamar al LLM, en la cabecera.
        if budget is not None:
            spent = await conversation_spent(conversation_id) + turn_cost
            if spent >= budget:
                await _mark_budget_exhausted(conversation_id)
                closure = await load_response(spec, "budget_exhausted") or ""
                await _write_usage_ledger(conversation_id, spec, usage, turn_span)
                return TurnResult(text=closure) if closure else None
        try:
            with tracer.start_as_current_span("llm_call") as llm_span:
                response = await provider.generate(
                    system_blocks=system_blocks,
                    messages=messages,
                    tools=tool_schemas,
                    max_tokens=settings.llm_max_tokens,
                    temperature=temperature,
                )
                span_attributes(
                    llm_span,
                    model=response.model,
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                    cost_usd=_response_cost(provider, response),
                )
        except LLMProviderError:
            # v2.0.0 (ADR-0050): fallback real. Si el proveedor/modelo primario
            # falla y el tenant definió `spec.model.fallback` (distinto), se
            # intenta UNA vez con ese modelo antes de escalar.
            fallback_model = spec.model.fallback
            recovered = None
            if fallback_model and fallback_model != routing.model:
                try:
                    fallback_provider = _resolved_provider_fallback(app_settings, fallback_model)
                    recovered = await fallback_provider.generate(
                        system_blocks=system_blocks,
                        messages=messages,
                        tools=tool_schemas,
                        max_tokens=settings.llm_max_tokens,
                        temperature=temperature,
                    )
                    provider = fallback_provider
                    log.warning(
                        "llm_call_fallback_ok",
                        conversation_id=str(conversation_id),
                        fallback=fallback_model,
                    )
                except LLMProviderError:
                    log.exception("llm_call_fallback_failed", conversation_id=str(conversation_id))
            if recovered is None:
                log.exception("llm_call_failed", conversation_id=str(conversation_id))
                await escalate(conversation_id, reason="llm_error", summary="Fallo del proveedor LLM")
                usage.escalated = True
                await _write_usage_ledger(conversation_id, spec, usage, turn_span)
                fallback_text = await load_response(spec, "fallback") or FALLBACK_REPLY
                return TurnResult(text=fallback_text, escalated=True)
            response = recovered

        turn_cost += _response_cost(provider, response)
        usage.add(provider, response)

        # ---- tool_use
        if response.stop_reason == "tool_use":
            messages.append(
                LLMMessage(role="assistant", content=response.content, tool_calls=response.tool_calls)
            )
            for tool_call in response.tool_calls:
                with tracer.start_as_current_span("tool_execute") as tool_span:
                    result = await _execute_tool_safely(conversation, customer, tool_call, spec)
                    span_attributes(tool_span, tool=tool_call.name)
                # _tool_name (reservada, safe_renderer.py): guardrails como
                # crm_completion_check necesitan saber QUÉ tool produjo cada
                # resultado para verificar que una afirmación de "ya lo hice"
                # en el texto corresponda a una tool REALMENTE llamada, no solo
                # a que algún resultado exista.
                all_tool_results.append({**result, TOOL_NAME_KEY: tool_call.name})
                usage.tool_calls.append({"tool": tool_call.name})
                messages.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        content=mcp_host.serialize_result(strip_reserved_keys(result)),
                    )
                )
            continue

        # ---- refusal
        if response.stop_reason == "refusal":
            await escalate(conversation_id, reason="llm_refusal", summary=latest_message[:200])
            usage.escalated = True
            await _write_usage_ledger(conversation_id, spec, usage, turn_span)
            escalation_text = await load_response(spec, "escalation") or ESCALATION_REPLY
            return TurnResult(text=escalation_text, escalated=True)

        # ---- end_turn: guardrails
        # Se siembra con tool_results de turnos ANTERIORES (audit_log) + los del
        # turno actual -- sin esto, un guardrail como _amount_guardrail solo veía
        # lo llamado en ESTE turno y escalaba conversaciones válidas cada vez que
        # el asistente repetía/confirmaba un monto ya verificado antes (mismo bug
        # que agent-sale ya había encontrado y resuelto en priceGuardrail.ts).
        # OJO: no se mezcla con `all_tool_results` (ese sigue siendo solo del
        # turno actual) porque ese sí alimenta `extract_safe_extras` más abajo --
        # si se mezclara ahí, un _safe_append viejo (ej. un link de pago vencido)
        # podría reaparecer en la respuesta de un turno nuevo sin relación.
        # v2.0.0 (ADR-0050): validaciones de persona del spec, ANTES de los
        # guardrails de packs. `forbiddenPhrases` (frases prohibidas) y
        # `maxLength` (tope de longitud) usan el mismo mecanismo de reintento
        # correctivo -> escalar.
        persona_verdict = _persona_guard(response.content, spec)
        # Reglas declarativas del tenant (ADR-0051): `spec.extensions["guardrails"]`.
        custom_rules = (spec.extensions or {}).get("guardrails") if spec.extensions else None
        verdict = persona_verdict or await run_guardrails(
            response.content,
            [*history_tool_results, *all_tool_results],
            enabled,
            disabled_guardrails,
            custom_rules=custom_rules,
        )
        if not verdict.passed:
            if guardrail_retries < guardrail_max_retries:
                guardrail_retries += 1
                messages.append(LLMMessage(role="assistant", content=response.content))
                messages.append(LLMMessage(role="user", content=f"[SISTEMA] {verdict.retry_instruction}"))
                continue
            # `verdict.reason` puede traer fragmentos de la respuesta del LLM
            # (montos, datos del cliente): se persiste solo el identificador del
            # guardrail, no el texto completo (evita PII en `escalations.summary`).
            guardrail_id = verdict.reason.split(":", 1)[0][:120]
            await escalate(conversation_id, reason="guardrail", summary=guardrail_id)
            usage.escalated = True
            await _write_usage_ledger(conversation_id, spec, usage, turn_span)
            escalation_text = await load_response(spec, "escalation") or ESCALATION_REPLY
            return TurnResult(text=escalation_text, escalated=True)

        # ---- respuesta final
        extras = extract_safe_extras(all_tool_results)
        final_text = apply_safe_extras(strip_markdown(response.content), extras)
        unresolved = await _finish_turn(
            conversation_id,
            final_text,
            latest_message,
            resolved=bool(all_tool_results),
            short_term_turns=spec.memory.shortTermTurns,
        )
        await _write_usage_ledger(conversation_id, spec, usage, turn_span)
        # Autoescalado por turnos sin resolver (ADR-0051, operativo en AppSettings).
        auto_after = (app_settings.escalation_config or {}).get("auto_escalate_after_turns") if app_settings else None
        try:
            threshold = min(20, int(auto_after)) if auto_after else 0
        except (TypeError, ValueError):
            threshold = 0
        if threshold > 0 and unresolved >= threshold:
            await escalate(
                conversation_id, reason="turns_unresolved", summary=f"{unresolved} turnos sin resolver"
            )
            usage.escalated = True
            escalation_text = await load_response(spec, "escalation") or ESCALATION_REPLY
            return TurnResult(text=final_text + "\n\n" + escalation_text, escalated=True, image_url=extras.image_url)
        return TurnResult(text=final_text, image_url=extras.image_url)

    # Se agotaron las iteraciones
    await escalate(conversation_id, reason="max_iterations", summary="Loop sin resolución")
    usage.escalated = True
    await _write_usage_ledger(conversation_id, spec, usage, turn_span)
    escalation_text = await load_response(spec, "escalation") or ESCALATION_REPLY
    return TurnResult(text=escalation_text, escalated=True)


# ------------------------------------------------------------------ helpers
def _persona_guard(response_text: str, spec) -> GuardrailResult | None:  # type: ignore[no-untyped-def]
    """Validaciones de persona del spec (ADR-0050): frases prohibidas y longitud.

    Devuelve un `GuardrailResult` fallido (con instrucción correctiva) o None si
    la respuesta pasa. Es barato y determinista, por eso corre antes de los
    guardrails de packs.
    """
    lowered = response_text.lower()
    for idx, phrase in enumerate(spec.persona.forbiddenPhrases):
        if phrase and phrase.lower() in lowered:
            # No se interpola la frase cruda en la instrucción (evita inyectar
            # contenido admin-controlado con prefijo [SISTEMA]): se referencia por
            # posición.
            return GuardrailResult(
                passed=False,
                reason="persona_forbidden_phrase",
                retry_instruction=(
                    "Tu respuesta usó una frase prohibida para este negocio "
                    f"(la #{idx + 1} de la lista). Reformulala sin frases prohibidas."
                ),
            )
    if spec.persona.maxLength and len(response_text) > spec.persona.maxLength:
        return GuardrailResult(
            passed=False,
            reason="persona_max_length",
            retry_instruction=(
                f"Tu respuesta supera el largo máximo permitido ({spec.persona.maxLength} "
                "caracteres). Resumila sin perder información importante."
            ),
        )
    return None


async def _execute_tool_safely(
    conversation: Conversation, customer: Customer, tool_call: LLMToolCall, spec=None  # type: ignore[no-untyped-def]
) -> dict[str, Any]:
    """Ejecuta una tool aislando el error: un plugin roto se desactiva, el turno sigue."""
    from core.mcp.declarative import tools_for_spec
    from core.mcp.types import ScopedHttp
    from core.plugin_manager import plugin_manager

    # ScopedHttp (ADR-0052): única salida a red. Se acota a la allowlist de LA tool
    # que se ejecuta (no la unión de todas), para que una tool no pueda llamar a
    # dominios autorizados para otra. Una tool Python que no use `context.http` no
    # queda afectada.
    declarative = {t.name: t for t in tools_for_spec(spec)} if spec is not None else {}
    allowed = list(getattr(declarative.get(tool_call.name), "_allowed_domains", []) or [])
    context = new_tool_context(
        conversation_id=conversation.id,
        customer_id=customer.id,
        idempotency_key=f"{conversation.id}:{tool_call.id}",
        channel=conversation.channel,
        http=ScopedHttp(allowed),
    )

    # Enablement + consentimiento de la tool por tenant (ADR-0019). Aunque las
    # skills se filtran en el prompt, un tool_call fuera de schema (p. ej.
    # inyección) no debe ejecutar tools de un pack que el tenant no tiene, ni
    # una tool deshabilitada por binding. La denegación es un error de negocio
    # limpio, SIN auto_disable.
    if spec is not None:
        denial = await _tool_denial(spec, tool_call.name, conversation.id)
        if denial is not None:
            result = {"error": denial}
            await record_audit(
                actor=f"tool:{tool_call.name}",
                action="denied",
                conversation_id=conversation.id,
                input_data=tool_call.arguments,
                output_data=result,
            )
            return result

    # v2.0.0 (ADR-0050): human-in-the-loop. Un binding con `requiresConfirmation`
    # no ejecuta de una: la primera vez deja el pedido pendiente y se lo devuelve
    # al modelo para que pida confirmación al cliente; la siguiente llamada con
    # los MISMOS argumentos (el cliente ya dijo "sí") sí ejecuta.
    # El objeto `conversation` se actualiza en memoria para que la repetición
    # dentro del MISMO turno ya cuente como confirmada (no re-lee de BD).
    if spec is not None and _binding_requires_confirmation(spec, tool_call.name):
        pending = dict(conversation.state.get("pending_confirmation") or {})
        already = (
            pending.get("tool") == tool_call.name
            and pending.get("arguments") == tool_call.arguments
        )
        if not already:
            state = dict(conversation.state)
            state["pending_confirmation"] = {
                "tool": tool_call.name,
                "arguments": tool_call.arguments,
            }
            conversation.state = state
            async with get_session() as session:
                row = await session.get(Conversation, conversation.id)
                if row is not None:
                    row.state = state
            result = {
                "status": "confirmation_required",
                "message": (
                    f"Antes de ejecutar '{tool_call.name}' pedile confirmación explícita "
                    "al cliente y, si acepta, volvé a llamar la herramienta con los "
                    "mismos argumentos."
                ),
            }
            await record_audit(
                actor=f"tool:{tool_call.name}",
                action="confirmation_required",
                conversation_id=conversation.id,
                input_data=tool_call.arguments,
                output_data=result,
            )
            return result
        # Ya confirmada: se ejecuta y se limpia el pendiente (no queda vivo para
        # siempre -- una llamada futura pide confirmación de nuevo).
        state = dict(conversation.state)
        state.pop("pending_confirmation", None)
        conversation.state = state
        async with get_session() as session:
            row = await session.get(Conversation, conversation.id)
            if row is not None:
                row.state = state

    try:
        # Prioridad al host: una tool declarativa NO puede shadowear una tool
        # registrada (evita que un tenant redefina una tool nativa/pack).
        if tool_call.name in declarative and mcp_host.get_tool(tool_call.name) is None:
            # Tool declarativa del tenant (ADR-0052): se ejecuta con su handler
            # (ScopedHttp acotado), no vía mcp_host (que no la conoce).
            result = await declarative[tool_call.name].handler(tool_call.arguments, context)
        else:
            result = await mcp_host.execute_tool(tool_call.name, tool_call.arguments, context)
    except PluginError as exc:
        # Las tools del CORE ("core") no se auto-desactivan: el core no se apaga a sí mismo.
        if exc.plugin_id != "core":
            # Acotado al tenant de ESTA conversación (ADR-0024): no apaga el
            # plugin para los demás tenants que nunca tuvieron el problema.
            await plugin_manager.auto_disable_for_tenant(get_current_tenant_id(), exc.plugin_id, str(exc))
        result = {
            "error": "La herramienta no está disponible en este momento. "
            "Discúlpate y ofrece escalar a un asesor humano si es necesario."
        }
    await record_audit(
        actor=f"tool:{tool_call.name}",
        action="execute",
        conversation_id=conversation.id,
        input_data=tool_call.arguments,
        # Se incluye `_tool_name` para que `_historical_tool_results` pueda
        # reconstruir qué tools se llamaron en turnos anteriores (lo usa el
        # guardrail declarativo `required_tool`, ADR-0051).
        output_data={**result, TOOL_NAME_KEY: tool_call.name},
    )
    return result


async def _connections_ok(tool, connected_types: set[str]) -> bool:  # type: ignore[no-untyped-def]
    """True si la tool puede ofrecerse según las conexiones del tenant (ADR-0072).

    Si la tool declara `connection_predicate` (ADR-0084), es ella quien decide POR
    REQUEST (requisito dependiente de la config del pack, p. ej. pack-calendar en
    modo `interno` vs `google`). Un predicado que falla deniega (fail-closed): no se
    ofrece una tool cuyo requisito no pudimos evaluar.
    """
    predicate = getattr(tool, "connection_predicate", None)
    if predicate is not None:
        try:
            return bool(await predicate(connected_types))
        except Exception:
            return False
    required = getattr(tool, "requires_connection_types", None) or []
    return all(t in connected_types for t in required)


async def _tool_denial(
    spec, tool_name: str, conversation_id: uuid.UUID | None = None
) -> str | None:
    """Razón de denegación de una tool según el AgentSpec (ADR-0019/ADR-0050), o None.

    - La tool de un pack NO habilitado se deniega: defensa en profundidad, el
      prompt ya filtra skills pero un tool_call fuera de schema (inyección) no
      debe ejecutar tools de un pack que el tenant no tiene.
    - La tool de un plugin con salud rota PARA ESTE TENANT (ADR-0024) también
      se deniega acá -- mismo camino de defensa en profundidad que el punto
      anterior, para un tool_call fuera de schema.
    - Si hay ToolBinding con enabled=false se deniega. Fail-open transitorio:
      sin binding, la tool corre (las migraciones de bindings van después).
    - v2.0.0 (ADR-0050): un binding con `maxCallsPerConversation` limita cuántas
      veces puede llamarse esa tool en la MISMA conversación (corta abuso/costo);
      `scopes` es autorización fina que un guardrail/pack puede exigir.
    """
    from core.mcp.declarative import tools_for_spec
    from core.plugin_manager import plugin_manager

    tool = mcp_host.get_tool(tool_name)
    declarative_names = {t.name for t in tools_for_spec(spec)}
    if tool is None and tool_name not in declarative_names:
        # No es una tool registrada ni declarativa del tenant: no debe ejecutarse
        # (defensa en profundidad ante un tool_call fuera de schema).
        return f"La herramienta '{tool_name}' no está disponible para este tenant."
    if tool is not None:
        if tool.plugin_id not in enabled_packs(spec):
            return f"La herramienta '{tool_name}' no está disponible para este tenant."
        tenant_id = get_current_tenant_id()
        if tenant_id is not None and await plugin_manager.is_disabled_for_tenant(tenant_id, tool.plugin_id):
            return f"La herramienta '{tool_name}' no está disponible en este momento."
        predicate = getattr(tool, "connection_predicate", None)
        if predicate is not None:
            # Requisito config-dependente (ADR-0084): se reevalúa a mitad de turno.
            from core.gateway.connections import active_connection_type_ids

            connected = await active_connection_type_ids(tenant_id)
            try:
                ok = bool(await predicate(connected))
            except Exception:
                ok = False
            if not ok:
                return f"La herramienta '{tool_name}' no está disponible en este momento."
        elif tool.requires_connection_types:
            # Revocación a mitad de turno (ADR-0072): el pre-prompt ya la filtró,
            # pero un tool_call que llegó con la conexión ya caída se deniega acá.
            from core.gateway.connections import active_connection_type_ids

            connected = await active_connection_type_ids(tenant_id)
            missing = [rt for rt in tool.requires_connection_types if rt not in connected]
            if missing:
                return (
                    f"La herramienta '{tool_name}' no está disponible: falta conectar "
                    f"{', '.join(missing)}."
                )
    for binding in spec.tools:
        if binding.tool != tool_name:
            continue
        if not binding.enabled:
            return f"La herramienta '{tool_name}' está deshabilitada para este tenant."
        # v2.0.0 (ADR-0050): autorización fina. Si la tool declara scopes que
        # requiere y el binding del tenant no los concede, se deniega.
        if tool is not None and tool.scopes:
            granted = set(binding.scopes or [])
            missing = [s for s in tool.scopes if s not in granted]
            if missing:
                return (
                    f"La herramienta '{tool_name}' requiere permisos que este negocio "
                    f"no tiene concedidos ({', '.join(missing)})."
                )
        if binding.maxCallsPerConversation is not None and conversation_id is not None:
            used = await _tool_calls_in_conversation(conversation_id, tool_name)
            if used >= binding.maxCallsPerConversation:
                return (
                    f"Llegaste al máximo de llamadas a '{tool_name}' en esta conversación "
                    f"({binding.maxCallsPerConversation})."
                )
    return None


def _binding_requires_confirmation(spec, tool_name: str) -> bool:  # type: ignore[no-untyped-def]
    for binding in spec.tools:
        if binding.tool == tool_name:
            return bool(binding.requiresConfirmation)
    return False


async def _tool_calls_in_conversation(conversation_id: uuid.UUID, tool_name: str) -> int:
    """Cuántas veces se EJECUTÓ `tool_name` en esta conversación (audit_log, ADR-0050).

    Solo cuenta `action="execute"`: las denegaciones y los pedidos de confirmación
    no deben consumir el cupo de `maxCallsPerConversation`.
    """
    async with get_session() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.tenant_id == get_current_tenant_id(),
                    AuditLog.conversation_id == conversation_id,
                    AuditLog.actor == f"tool:{tool_name}",
                    AuditLog.action == "execute",
                )
            )
        ).scalar_one()


async def _historical_tool_results(conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    """Resultados de tools de turnos ANTERIORES de esta conversación.

    `_load_history` no sirve para esto: colapsa la historia a texto plano
    user/assistant y descarta el JSON de cada tool_result. `audit_log` es la
    única fuente que los conserva (insert-only, ADR de auditoría) -- se usa
    solo para alimentar guardrails como `_amount_guardrail`, nunca para
    construir el prompt ni la respuesta final.
    """
    async with get_session() as session:
        result = await session.execute(
            select(AuditLog.output)
            .where(
                # tenant_id explícito -- defensa en profundidad además de RLS,
                # mismo criterio que el resto de queries sensibles del repo
                # (auditor-multitenant, 2026-09-11).
                AuditLog.tenant_id == get_current_tenant_id(),
                AuditLog.conversation_id == conversation_id,
                AuditLog.actor.like("tool:%"),
                AuditLog.output.is_not(None),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(40)
        )
        return [row for row in result.scalars().all() if row is not None]


async def _load_history(conversation_id: uuid.UUID) -> list[LLMMessage]:
    """Working memory (Redis) con fallback a Postgres."""
    turns = await working_memory.get_recent_turns(conversation_id)
    if turns:
        return [
            LLMMessage(role=t["role"], content=t["content"])
            for t in turns
            if t["role"] in ("user", "assistant") and t.get("content")
        ]

    async with get_session() as session:
        result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                # tenant_id explícito -- defensa en profundidad además de RLS.
                Message.tenant_id == get_current_tenant_id(),
            )
            .order_by(Message.created_at)
            .limit(40)
        )
        rows = result.scalars().all()
    history: list[LLMMessage] = []
    for row in rows:
        role = "user" if row.direction == "inbound" else "assistant"
        if row.content:
            history.append(LLMMessage(role=role, content=row.content))
    return history


async def _finish_turn(
    conversation_id: uuid.UUID,
    response_text: str,
    user_message: str,
    resolved: bool,
    short_term_turns: int | None = None,
) -> int:
    """Cierra el turno y devuelve `turnos_sin_resolver` (para autoescalado, ADR-0051)."""
    await working_memory.append_turn(
        conversation_id, "user", user_message, max_turns=short_term_turns
    )
    await working_memory.append_turn(
        conversation_id, "assistant", response_text, max_turns=short_term_turns
    )

    unresolved = 0
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            state = dict(conversation.state)
            unresolved = 0 if resolved else int(state.get("turnos_sin_resolver", 0)) + 1
            state["turnos_sin_resolver"] = unresolved
            conversation.state = state

    # Memoria semántica: guardar el intercambio para recall futuro (best-effort)
    await vector_memory.store_memory(
        content=f"Cliente: {user_message[:300]} | Asistente: {response_text[:300]}",
        conversation_id=conversation_id,
    )
    return unresolved


def _response_cost(provider, response) -> float:  # type: ignore[no-untyped-def]
    """Costo USD de una respuesta según el pricing del provider que la sirvió.

    Antes esto importaba SIEMPRE el pricing de openai_compatible, sin mirar
    qué provider respondió. Con Anthropic activo, response.model nunca
    matcheaba ese dict y el costo quedaba en $0.00 — silenciosamente mal en
    usage_ledger, la fuente de facturación (ADR-0008).
    """
    return provider.estimate_cost_usd(response.usage.input_tokens, response.usage.output_tokens)


async def _mark_budget_exhausted(conversation_id: uuid.UUID) -> None:
    """Marca la conversación para que los próximos inbound no gasten LLM (ADR-0015)."""
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            state = dict(conversation.state)
            state["budget_exhausted"] = True
            conversation.state = state


def _resolved_model(app_settings: AppSettings | None, spec, env_model: str) -> str:  # type: ignore[no-untyped-def]
    """Prioridad: BYOK del panel > spec.model.primary > env."""
    if app_settings and app_settings.llm_model:
        return app_settings.llm_model
    if spec is not None and spec.model.primary:
        return spec.model.primary
    return env_model


def _resolved_provider(app_settings: AppSettings | None, model: str):  # type: ignore[no-untyped-def]
    """BYOK: la config de BD (panel) tiene prioridad sobre el .env."""
    provider_key = None
    api_key = None
    if app_settings and app_settings.llm_provider:
        provider_key = app_settings.llm_provider
        if app_settings.llm_api_key_encrypted:
            api_key = get_secret_box().decrypt(app_settings.llm_api_key_encrypted)
    return resolve_provider(provider_key=provider_key, api_key=api_key, model=model)


def _resolved_provider_fallback(app_settings: AppSettings | None, model: str):  # type: ignore[no-untyped-def]
    """Provider para el modelo de fallback (ADR-0050).

    Reusa la MISMA config del primario (BYOK del panel o .env) y solo cambia el
    modelo. No se clona el provider: se resuelve de nuevo con
    `resolve_provider`, que arma un provider válido para ese modelo.
    """
    provider_key = None
    api_key = None
    if app_settings and app_settings.llm_provider:
        provider_key = app_settings.llm_provider
        if app_settings.llm_api_key_encrypted:
            api_key = get_secret_box().decrypt(app_settings.llm_api_key_encrypted)
    return resolve_provider(provider_key=provider_key, api_key=api_key, model=model)

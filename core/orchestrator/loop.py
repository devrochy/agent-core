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

from sqlalchemy import select

from core.agents.responses import load_response
from core.agents.spec import enabled_packs, load_active_spec
from core.config import get_settings
from core.db.models import AppSettings, AuditLog, Conversation, Customer, Message, UsageLedger
from core.db.session import get_session
from core.llm import resolve_provider
from core.llm.difficulty_router import route_model
from core.llm.prompt_builder import PromptContext, build_system_blocks
from core.llm.types import LLMMessage, LLMToolCall
from core.mcp.host import mcp_host, new_tool_context
from core.mcp.translator import skills_summary, to_llm_schemas
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

    # Presupuesto agotado (ADR-0015): no se gasta más LLM en esta conversación
    # hasta que un admin suba el presupuesto o lo resetee.
    if conversation.state.get("budget_exhausted"):
        return None

    # ---------------- 1. Memoria y contexto
    history = await _load_history(conversation_id)
    recent_context = " ".join(m.content for m in history[-6:] if m.content)
    history_tool_results = await _historical_tool_results(conversation_id)

    profile = await entity_memory.get_profile(customer.id)
    recalled = await vector_memory.recall(latest_message, customer_id=customer.id)

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
    selected_names = select_relevant_skills(latest_message, recent_context, available_skills)
    active_tools = mcp_host.get_skill_tools(selected_names)

    prompt_ctx = PromptContext(
        assistant_name=app_settings.assistant_name if app_settings else settings.app_name,
        business_description=app_settings.business_description if app_settings else "",
        language=app_settings.language if app_settings else "es",
        tone=spec.persona.tone,
        brand_voice=app_settings.brand_voice if app_settings else "",
        active_skills_summary=skills_summary(active_tools),
        recalled_memories=recalled,
        entity_profile=entity_memory.profile_to_prompt(profile),
    )
    system_blocks = await build_system_blocks(spec, prompt_ctx)

    # ---------------- 2. Proveedor y routing
    turnos_sin_resolver = int(conversation.state.get("turnos_sin_resolver", 0))
    routing = route_model(
        message=latest_message,
        turnos_sin_resolver=turnos_sin_resolver,
        default_model=_resolved_model(app_settings, spec, settings.llm_model),
        reasoning_model=settings.llm_model_reasoning,
        routing_enabled=settings.difficulty_routing,
        requires_tools=bool(active_tools),
    )
    provider = _resolved_provider(app_settings, routing.model)
    tool_schemas = to_llm_schemas(active_tools) if routing.allow_tools else None

    # Reintentos de guardrail antes de escalar de verdad (pedido explícito del
    # usuario: "intentos fallidos" configurable por tenant, mismo patrón exacto
    # que debounce_mode -- JSONB libre en AppSettings, sin tocar el contrato
    # AgentSpec congelado). Fallback al default global si el tenant no lo seteó.
    behavior = dict(app_settings.behavior_config) if app_settings else {}
    # Clamp acá también (no solo en el form del admin, auditor-multitenant
    # 2026-09-12): behavior_config es JSONB libre, puede llegar por otra vía
    # (script, migración, endpoint futuro) sin pasar por la validación del form.
    guardrail_max_retries = max(
        0, min(5, int(behavior.get("guardrail_max_retries", MAX_GUARDRAIL_RETRIES)))
    )

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

    for _iteration in range(spec.policies.maxToolCallsPerTurn):
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
                )
                span_attributes(
                    llm_span,
                    model=response.model,
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                    cost_usd=_response_cost(provider, response),
                )
        except LLMProviderError:
            log.exception("llm_call_failed", conversation_id=str(conversation_id))
            await escalate(conversation_id, reason="llm_error", summary="Fallo del proveedor LLM")
            usage.escalated = True
            await _write_usage_ledger(conversation_id, spec, usage, turn_span)
            return TurnResult(text=FALLBACK_REPLY, escalated=True)

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
            return TurnResult(text=ESCALATION_REPLY, escalated=True)

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
        verdict = await run_guardrails(
            response.content, [*history_tool_results, *all_tool_results], enabled
        )
        if not verdict.passed:
            if guardrail_retries < guardrail_max_retries:
                guardrail_retries += 1
                messages.append(LLMMessage(role="assistant", content=response.content))
                messages.append(LLMMessage(role="user", content=f"[SISTEMA] {verdict.retry_instruction}"))
                continue
            await escalate(conversation_id, reason="guardrail", summary=verdict.reason)
            usage.escalated = True
            await _write_usage_ledger(conversation_id, spec, usage, turn_span)
            return TurnResult(text=ESCALATION_REPLY, escalated=True)

        # ---- respuesta final
        extras = extract_safe_extras(all_tool_results)
        final_text = apply_safe_extras(strip_markdown(response.content), extras)
        await _finish_turn(conversation_id, final_text, latest_message, resolved=bool(all_tool_results))
        await _write_usage_ledger(conversation_id, spec, usage, turn_span)
        return TurnResult(text=final_text, image_url=extras.image_url)

    # Se agotaron las iteraciones
    await escalate(conversation_id, reason="max_iterations", summary="Loop sin resolución")
    usage.escalated = True
    await _write_usage_ledger(conversation_id, spec, usage, turn_span)
    return TurnResult(text=ESCALATION_REPLY, escalated=True)


# ------------------------------------------------------------------ helpers
async def _execute_tool_safely(
    conversation: Conversation, customer: Customer, tool_call: LLMToolCall, spec=None  # type: ignore[no-untyped-def]
) -> dict[str, Any]:
    """Ejecuta una tool aislando el error: un plugin roto se desactiva, el turno sigue."""
    from core.plugin_manager import plugin_manager

    context = new_tool_context(
        conversation_id=conversation.id,
        customer_id=customer.id,
        idempotency_key=f"{conversation.id}:{tool_call.id}",
        channel=conversation.channel,
    )

    # Enablement + consentimiento de la tool por tenant (ADR-0019). Aunque las
    # skills se filtran en el prompt, un tool_call fuera de schema (p. ej.
    # inyección) no debe ejecutar tools de un pack que el tenant no tiene, ni
    # una tool deshabilitada por binding. La denegación es un error de negocio
    # limpio, SIN auto_disable.
    if spec is not None:
        denial = await _tool_denial(spec, tool_call.name)
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

    try:
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
        output_data=result,
    )
    return result


async def _tool_denial(spec, tool_name: str) -> str | None:
    """Razón de denegación de una tool según el AgentSpec (ADR-0019), o None si corre.

    - La tool de un pack NO habilitado se deniega: defensa en profundidad, el
      prompt ya filtra skills pero un tool_call fuera de schema (inyección) no
      debe ejecutar tools de un pack que el tenant no tiene.
    - La tool de un plugin con salud rota PARA ESTE TENANT (ADR-0024) también
      se deniega acá -- mismo camino de defensa en profundidad que el punto
      anterior, para un tool_call fuera de schema.
    - Si hay ToolBinding con enabled=false se deniega. Fail-open transitorio:
      sin binding, la tool corre (las migraciones de bindings van después).
    """
    from core.plugin_manager import plugin_manager

    tool = mcp_host.get_tool(tool_name)
    if tool is not None:
        if tool.plugin_id not in enabled_packs(spec):
            return f"La herramienta '{tool_name}' no está disponible para este tenant."
        tenant_id = get_current_tenant_id()
        if tenant_id is not None and await plugin_manager.is_disabled_for_tenant(tenant_id, tool.plugin_id):
            return f"La herramienta '{tool_name}' no está disponible en este momento."
    for binding in spec.tools:
        if binding.tool == tool_name and not binding.enabled:
            return f"La herramienta '{tool_name}' está deshabilitada para este tenant."
    return None


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
            .where(Message.conversation_id == conversation_id)
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
    conversation_id: uuid.UUID, response_text: str, user_message: str, resolved: bool
) -> None:
    await working_memory.append_turn(conversation_id, "user", user_message)
    await working_memory.append_turn(conversation_id, "assistant", response_text)

    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            state = dict(conversation.state)
            state["turnos_sin_resolver"] = 0 if resolved else int(state.get("turnos_sin_resolver", 0)) + 1
            conversation.state = state

    # Memoria semántica: guardar el intercambio para recall futuro (best-effort)
    await vector_memory.store_memory(
        content=f"Cliente: {user_message[:300]} | Asistente: {response_text[:300]}",
        conversation_id=conversation_id,
    )


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

"""Agentic Loop: el motor del asistente.

Por turno (hasta MAX_AGENT_ITERATIONS vueltas):
  1. Recall de memoria (3 capas) + selección de skills + system blocks.
  2. Difficulty routing (deepseek-chat vs deepseek-reasoner).
  3. Llamada al LLM. Registro de tokens/costo/latencia en llm_usage.
  4. tool_use  -> ejecutar tools vía MCPHost (PluginError => auto-disable, el loop sigue).
  5. end_turn  -> guardrails -> ok? SafeExtras + respuesta final.
                            -> falla? 1 reintento correctivo; si reincide, escalar.
  6. refusal   -> escalar.

El LLM propone, las tools deciden (contra la BD), el core arbitra.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.db.models import AppSettings, Conversation, Customer, LLMUsage, Message
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
from core.orchestrator.escalation import ESCALATION_REPLY, escalate
from core.orchestrator.guardrails import MAX_GUARDRAIL_RETRIES, run_guardrails
from core.orchestrator.safe_renderer import (
    apply_safe_extras,
    extract_safe_extras,
    strip_reserved_keys,
)
from core.orchestrator.skill_router import select_relevant_skills
from core.shared.audit import record_audit
from core.shared.crypto import get_secret_box
from core.shared.exceptions import LLMProviderError, PluginError
from core.shared.logger import get_logger

log = get_logger(__name__)

FALLBACK_REPLY = (
    "Disculpa, estoy teniendo un inconveniente técnico. "
    "Ya avisé a nuestro equipo para que te atienda una persona."
)


@dataclass
class TurnResult:
    text: str
    image_url: str | None = None
    escalated: bool = False


async def process_conversation(conversation_id: uuid.UUID, latest_message: str) -> TurnResult | None:
    settings = get_settings()

    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        customer = await session.get(Customer, conversation.customer_id)
        app_settings = (await session.execute(select(AppSettings).limit(1))).scalar_one_or_none()

    if customer is None or conversation.bot_paused or conversation.status == "escalated":
        return None

    # ---------------- 1. Memoria y contexto
    history = await _load_history(conversation_id)
    recent_context = " ".join(m.content for m in history[-6:] if m.content)

    profile = await entity_memory.get_profile(customer.id)
    recalled = await vector_memory.recall(latest_message, customer_id=customer.id)

    available_skills = mcp_host.list_skills()
    selected_names = select_relevant_skills(latest_message, recent_context, available_skills)
    active_tools = mcp_host.get_skill_tools(selected_names)

    prompt_ctx = PromptContext(
        assistant_name=app_settings.assistant_name if app_settings else settings.app_name,
        business_description=app_settings.business_description if app_settings else "",
        language=app_settings.language if app_settings else "es",
        tone=str((app_settings.behavior_config if app_settings else {}).get("tono", "calido")),
        brand_voice=app_settings.brand_voice if app_settings else "",
        active_skills_summary=skills_summary(active_tools),
        recalled_memories=recalled,
        entity_profile=entity_memory.profile_to_prompt(profile),
    )
    system_blocks = build_system_blocks(prompt_ctx)

    # ---------------- 2. Proveedor y routing
    turnos_sin_resolver = int(conversation.state.get("turnos_sin_resolver", 0))
    routing = route_model(
        message=latest_message,
        turnos_sin_resolver=turnos_sin_resolver,
        default_model=_resolved_model(app_settings, settings.llm_model),
        reasoning_model=settings.llm_model_reasoning,
        routing_enabled=settings.difficulty_routing,
        requires_tools=bool(active_tools),
    )
    provider = _resolved_provider(app_settings, routing.model)
    tool_schemas = to_llm_schemas(active_tools) if routing.allow_tools else None

    # ---------------- 3. Agentic loop
    messages = list(history)
    all_tool_results: list[dict[str, Any]] = []
    guardrail_retries = 0

    for _iteration in range(settings.max_agent_iterations):
        try:
            response = await provider.generate(
                system_blocks=system_blocks,
                messages=messages,
                tools=tool_schemas,
                max_tokens=settings.llm_max_tokens,
            )
        except LLMProviderError:
            log.exception("llm_call_failed", conversation_id=str(conversation_id))
            await escalate(conversation_id, reason="llm_error", summary="Fallo del proveedor LLM")
            return TurnResult(text=FALLBACK_REPLY, escalated=True)

        await _record_usage(conversation_id, provider.provider_name, response)

        # ---- tool_use
        if response.stop_reason == "tool_use":
            messages.append(
                LLMMessage(role="assistant", content=response.content, tool_calls=response.tool_calls)
            )
            for tool_call in response.tool_calls:
                result = await _execute_tool_safely(conversation, customer, tool_call)
                all_tool_results.append(result)
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
            return TurnResult(text=ESCALATION_REPLY, escalated=True)

        # ---- end_turn: guardrails
        verdict = await run_guardrails(response.content, all_tool_results)
        if not verdict.passed:
            if guardrail_retries < MAX_GUARDRAIL_RETRIES:
                guardrail_retries += 1
                messages.append(LLMMessage(role="assistant", content=response.content))
                messages.append(LLMMessage(role="user", content=f"[SISTEMA] {verdict.retry_instruction}"))
                continue
            await escalate(conversation_id, reason="guardrail", summary=verdict.reason)
            return TurnResult(text=ESCALATION_REPLY, escalated=True)

        # ---- respuesta final
        extras = extract_safe_extras(all_tool_results)
        final_text = apply_safe_extras(response.content, extras)
        await _finish_turn(conversation_id, final_text, latest_message, resolved=bool(all_tool_results))
        return TurnResult(text=final_text, image_url=extras.image_url)

    # Se agotaron las iteraciones
    await escalate(conversation_id, reason="max_iterations", summary="Loop sin resolución")
    return TurnResult(text=ESCALATION_REPLY, escalated=True)


# ------------------------------------------------------------------ helpers
async def _execute_tool_safely(
    conversation: Conversation, customer: Customer, tool_call: LLMToolCall
) -> dict[str, Any]:
    """Ejecuta una tool aislando el error: un plugin roto se desactiva, el turno sigue."""
    from core.plugin_manager import plugin_manager

    context = new_tool_context(
        conversation_id=conversation.id,
        customer_id=customer.id,
        idempotency_key=f"{conversation.id}:{tool_call.id}",
        channel=conversation.channel,
    )
    try:
        result = await mcp_host.execute_tool(tool_call.name, tool_call.arguments, context)
    except PluginError as exc:
        await plugin_manager.auto_disable(exc.plugin_id, str(exc))
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


async def _record_usage(conversation_id: uuid.UUID, provider_name: str, response) -> None:  # type: ignore[no-untyped-def]
    try:
        from core.llm.openai_compatible import PRICING as OPENAI_PRICING

        cin, cout = OPENAI_PRICING.get(response.model, (0.0, 0.0))
        cost = (response.usage.input_tokens * cin + response.usage.output_tokens * cout) / 1_000_000
        async with get_session() as session:
            session.add(
                LLMUsage(
                    conversation_id=conversation_id,
                    provider=provider_name,
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    latency_ms=response.latency_ms,
                    cost_usd=cost,
                )
            )
    except Exception:
        log.warning("llm_usage_record_failed")  # best-effort: nunca interrumpe la respuesta


def _resolved_model(app_settings: AppSettings | None, env_model: str) -> str:
    if app_settings and app_settings.llm_model:
        return app_settings.llm_model
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

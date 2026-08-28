"""Runner genérico de guardrails.

Los plugins registran validaciones post-LLM vía MCPHost.register_guardrail().
El orchestrator las ejecuta ANTES de enviar cualquier respuesta al cliente:

  respuesta LLM -> guardrails -> ok? enviar
                              -> falla? 1 reintento con instrucción correctiva
                              -> falla de nuevo? escalar a humano

Presupuesto: 1 reintento de generación (mismo criterio que agent-sale).
Un guardrail que LANZA excepción se trata como PASSED (fail-open del guardrail,
fail-safe del asistente) y se loggea — un guardrail roto no debe silenciar al bot.
"""

from __future__ import annotations

from typing import Any

from core.mcp.host import mcp_host
from core.mcp.types import GuardrailResult
from core.shared.logger import get_logger

log = get_logger(__name__)

MAX_GUARDRAIL_RETRIES = 1


async def run_guardrails(
    response_text: str, tool_results: list[dict[str, Any]], enabled: set[str] | None = None
) -> GuardrailResult:
    """Ejecuta los guardrails del turno. `enabled` = packs del tenant (ADR-0019);
    solo corren los guardrails de packs habilitados."""
    guardrails = mcp_host.get_guardrails()
    if enabled is not None:
        guardrails = [g for g in guardrails if g.plugin_id in enabled]
    for guardrail in guardrails:
        try:
            result = await guardrail.fn(response_text, tool_results)
        except Exception:
            log.exception("guardrail_crashed", guardrail=guardrail.name)
            continue
        if not result.passed:
            log.warning("guardrail_failed", guardrail=guardrail.name, reason=result.reason)
            return GuardrailResult(
                passed=False,
                reason=f"{guardrail.name}: {result.reason}",
                retry_instruction=result.retry_instruction
                or "Tu respuesta anterior contenía información no verificada. "
                "Corrige usando SOLO datos devueltos por las herramientas.",
            )
    return GuardrailResult(passed=True)

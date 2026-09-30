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
    response_text: str,
    tool_results: list[dict[str, Any]],
    enabled: set[str] | None = None,
    disabled: set[str] | None = None,
    custom_rules: list[dict[str, Any]] | None = None,
) -> GuardrailResult:
    """Ejecuta los guardrails del turno. `enabled` = packs del tenant (ADR-0019);
    solo corren los guardrails de packs habilitados. `disabled` = nombres de
    guardrails individuales desactivados por este tenant (parametrización por
    tenant, `AppSettings.behavior_config["disabled_guardrails"]`) -- permite
    apagar un guardrail puntual (ej. `crm_amount_check`) sin apagar el pack
    entero. `custom_rules` = reglas declarativas del tenant (ADR-0051, DSL seguro)."""
    # 1. Reglas declarativas del tenant (ADR-0051): baratas y deterministas, van
    #    primero. DSL seguro, sin regex crudo (ADR-0025).
    declared = _eval_custom_rules(response_text, tool_results, custom_rules or [])
    if declared is not None:
        return declared

    guardrails = mcp_host.get_guardrails()
    if enabled is not None:
        guardrails = [g for g in guardrails if g.plugin_id in enabled]
    if disabled:
        guardrails = [g for g in guardrails if g.name not in disabled]
    for guardrail in guardrails:
        try:
            result = await guardrail.fn(response_text, tool_results)
        except Exception:
            log.exception("guardrail_crashed", guardrail=guardrail.name)
            continue
        if not result.passed:
            # No se loggea el `reason` completo: puede contener fragmentos de la
            # respuesta del LLM con contexto del cliente (montos, datos personales).
            log.warning("guardrail_failed", guardrail=guardrail.name)
            return GuardrailResult(
                passed=False,
                reason=f"{guardrail.name}: {result.reason}",
                retry_instruction=result.retry_instruction
                or "Tu respuesta anterior contenía información no verificada. "
                "Corrige usando SOLO datos devueltos por las herramientas.",
            )
    return GuardrailResult(passed=True)


# ------------------------------------------------------------------ reglas declarativas (ADR-0051)
# Kinds soportados (DSL seguro, sin regex crudo -- ADR-0025):
#   forbidden_phrase: `phrases` no deben aparecer en la respuesta (substring, ci).
#   required_tool:    si la respuesta aparece una frase de `phrases`, exige que
#                     `tool` se haya llamado (anti "ya lo hice" sin respaldo).
#   max_length:       la respuesta no supera `value` caracteres.
CUSTOM_RULE_KINDS = ("forbidden_phrase", "required_tool", "max_length")


def validate_custom_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normaliza/valida reglas declarativas; descarta las malformadas (fail-safe)."""
    clean: list[dict[str, Any]] = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        kind = str(rule.get("kind") or "")
        if kind not in CUSTOM_RULE_KINDS:
            continue
        action = str(rule.get("action") or "retry")
        if action not in ("retry", "escalate"):
            action = "retry"
        entry: dict[str, Any] = {
            "name": str(rule.get("name") or kind)[:60],
            "kind": kind,
            "action": action,
        }
        if kind in ("forbidden_phrase", "required_tool"):
            phrases = [str(p).strip() for p in (rule.get("phrases") or []) if str(p).strip()]
            if not phrases:
                continue
            entry["phrases"] = phrases[:50]
        if kind == "required_tool":
            tool = str(rule.get("tool") or "").strip()
            if not tool:
                continue
            entry["tool"] = tool
        if kind == "max_length":
            try:
                entry["value"] = max(1, int(rule.get("value") or 0))
            except (TypeError, ValueError):
                continue
        clean.append(entry)
    return clean


def _eval_custom_rules(
    response_text: str, tool_results: list[dict[str, Any]], rules: list[dict[str, Any]]
) -> GuardrailResult | None:
    """Evalúa las reglas declarativas del tenant. None = todas pasan."""
    from core.orchestrator.safe_renderer import TOOL_NAME_KEY

    lowered = response_text.lower()
    called_tools = {str(tr.get(TOOL_NAME_KEY) or "") for tr in tool_results}

    for rule in validate_custom_rules(rules):
        kind = rule["kind"]
        failed = False
        if kind == "forbidden_phrase":
            failed = any(p.lower() in lowered for p in rule["phrases"])
        elif kind == "required_tool":
            mentions = any(p.lower() in lowered for p in rule["phrases"])
            failed = mentions and rule["tool"] not in called_tools
        elif kind == "max_length":
            failed = len(response_text) > rule["value"]

        if failed:
            instruction = (
                "Tu respuesta no cumplió una regla del negocio. Corregila y reintentá."
                if rule["action"] == "retry"
                else "Tu respuesta violó una regla del negocio."
            )
            log.warning("custom_guardrail_failed", rule=rule["name"])
            return GuardrailResult(
                passed=False,
                reason=f"regla:{rule['name']}",
                retry_instruction=instruction,
            )
    return None

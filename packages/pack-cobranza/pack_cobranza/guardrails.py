"""Guardrails de pack-cobranza.

`promesa_guardrail`: evita que el asistente AFIRME haber registrado una promesa
o un acuerdo sin que la tool correspondiente haya corrido en el mismo turno —
mismo patrón que `crm_completion_check` de pack-commerce. No es infalible
(es texto libre), pero cierra el caso real: prometer un acuerdo que nunca se
persistió.
"""

from __future__ import annotations

import re
from typing import Any

from core.mcp.types import GuardrailResult

_PROMESA_CLAIM = re.compile(
    r"(registr[eé]|agend[eé]|dej[eé]\s+registrad|acordamos|qued[oó]\s+(registrad|agendad)|"
    r"prometiste|tu\s+promesa\s+qued)",
    re.IGNORECASE,
)


async def promesa_guardrail(
    response_text: str, tool_results: list[dict[str, Any]]
) -> GuardrailResult:
    ran = {r.get("_tool_name") for r in (tool_results or [])}
    # `cob_registrar_promesa_saldo` es la del procedimiento `promesa_de_pago` (HU-AOS-08).
    if ran & {"cob_registrar_promesa_pago", "cob_registrar_promesa_saldo"}:
        return GuardrailResult(passed=True)
    if _PROMESA_CLAIM.search(response_text or ""):
        return GuardrailResult(
            passed=False,
            reason="El asistente afirmó registrar una promesa/acuerdo sin ejecutar la tool.",
            retry_instruction=(
                "No afirmes que registraste una promesa ni un acuerdo. Si el cliente "
                "confirmó fecha y valor, llama `cob_registrar_promesa_pago`; si no, pide "
                "los datos que faltan."
            ),
        )
    return GuardrailResult(passed=True)


def _claim(pattern: str, tools: set[str]) -> tuple[re.Pattern[str], set[str]]:
    return re.compile(pattern, re.IGNORECASE), tools


# Afirmaciones sobre la suscripción (ADR-0082) que exigen la tool del turno.
_SUSCRIPCION_CLAIMS: dict[str, tuple[re.Pattern[str], set[str]]] = {
    "creada": _claim(
        r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:activ\w*|creada|registrada|contratada)|"
        r"(?:activ\w*|cre\w*|contrat\w*)\s+[^.]{0,30}suscripci[oó]n",
        {"cob_crear_suscripcion"},
    ),
    "pausada": _claim(
        r"suscripci[oó]n\s+(?:qued\w*\s+)?pausad\w*|paus\w+\s+(?:tu\s+)?suscripci",
        {"cob_pausar_suscripcion"},
    ),
    "cancelada": _claim(
        r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:cancel\w*|anulad\w*)|cancel\w+\s+(?:tu\s+)?suscripci",
        {"cob_cancelar_suscripcion"},
    ),
    "renovada": _claim(
        r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:renov\w*|reactivad\w*)|(?:renov\w*|reactiv\w+)\s+(?:tu\s+)?suscripci",
        {"cob_renovar_suscripcion"},
    ),
}


async def suscripcion_guardrail(
    response_text: str, tool_results: list[dict[str, Any]]
) -> GuardrailResult:
    """Bloquea afirmar cambios en la suscripción sin la tool correspondiente (ADR-0082)."""
    ran = {r.get("_tool_name") for r in (tool_results or [])}
    for label, (pattern, tools) in _SUSCRIPCION_CLAIMS.items():
        if pattern.search(response_text or "") and not (ran & tools):
            return GuardrailResult(
                passed=False,
                reason=f"Afirmación de suscripción '{label}' sin la tool correspondiente.",
                retry_instruction=(
                    f"Dijiste que la suscripción quedó '{label}' pero no llamaste la herramienta "
                    f"({', '.join(sorted(tools))}). Llamala ahora, o decí claramente que no se pudo."
                ),
            )
    return GuardrailResult(passed=True)

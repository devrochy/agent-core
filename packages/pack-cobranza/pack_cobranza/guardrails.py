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
    if "cob_registrar_promesa_pago" in ran:
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

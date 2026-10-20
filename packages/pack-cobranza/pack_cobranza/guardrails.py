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
from core.shared.polaridad import afirma_alguna

# La afirmación tiene que ser de cobranza: un verbo de registro junto a una promesa,
# acuerdo, compromiso, pago, abono o cuota, en la misma frase y en cualquier orden. Un
# "quedó agendada" a secas es de pack-calendar (incidente del 2026-10-19: en un negocio con
# los dos packs, cada cita bien creada se escalaba como promesa sin registrar).
_REGISTRO = (
    r"(?:registr[eé]|anot[eé]|dej[eé]\s+(?:registrad|anotad)\w*|qued[oó]\s+(?:registrad|anotad)\w*)"
)
_AGENDA = r"(?:agend[eé]|dej[eé]\s+agendad\w*|qued[oó]\s+agendad\w*)"
_COMPROMISO = r"(?:promesa|acuerdo|compromiso)s?"
_PAGO = r"(?:pag[oa]|abono|cuota)s?"


def _cerca(verbo: str, objeto: str) -> str:
    """`verbo` y `objeto` en la misma frase, a 40 caracteres como mucho, en cualquier orden."""
    return rf"\b{verbo}\b[^.!?\n]{{0,40}}?\b{objeto}\b|\b{objeto}\b[^.!?\n]{{0,40}}?\b{verbo}"


_PROMESA_CLAIM = re.compile(
    "|".join(
        (
            _cerca(_REGISTRO, rf"(?:{_COMPROMISO}|{_PAGO})"),
            _cerca(_AGENDA, _COMPROMISO),
            r"\bacordamos\b|\bprometiste\b|\bquedamos\s+en\s+que\b",
        )
    ),
    re.IGNORECASE,
)
# "Agendé tu pago": lo respalda también una cita creada en el turno (p. ej. una visita de
# cobro), porque el verbo es de calendario; un "registré" o un "acordamos" no.
_AGENDA_PAGO_CLAIM = re.compile(_cerca(_AGENDA, _PAGO), re.IGNORECASE)

_TOOLS_PROMESA = {"cob_registrar_promesa_pago", "cob_registrar_promesa_saldo"}
# Por nombre, sin importar pack_calendar (los packs no se importan entre sí).
_TOOLS_CITA = {"create_calendar_event", "update_calendar_event"}


def _cita_del_turno(tool_results: list[dict[str, Any]]) -> bool:
    """Una cita creada o movida con éxito en ESTE turno (no la de un turno anterior)."""
    return any(
        r.get("_tool_name") in _TOOLS_CITA
        and r.get("_current_turn") is not False
        and r.get("_ok") is not False
        for r in tool_results
    )


def _afirma_promesa(texto: str, tool_results: list[dict[str, Any]]) -> bool:
    if _PROMESA_CLAIM.search(texto):
        return True
    return bool(_AGENDA_PAGO_CLAIM.search(texto)) and not _cita_del_turno(tool_results)


async def promesa_guardrail(
    response_text: str, tool_results: list[dict[str, Any]]
) -> GuardrailResult:
    tool_results = tool_results or []
    ran = {r.get("_tool_name") for r in tool_results}
    # `cob_registrar_promesa_saldo` es la del procedimiento `promesa_de_pago` (HU-AOS-08).
    if ran & _TOOLS_PROMESA:
        return GuardrailResult(passed=True)
    if _afirma_promesa(response_text or "", tool_results):
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
    """Bloquea afirmar cambios en la suscripción sin la tool correspondiente (ADR-0082).

    Solo cuentan las oraciones que afirman (`core.shared.polaridad`): "No encontré una
    suscripción activa a tu nombre" o "¿Quieres activar tu suscripción?" no afirman nada. Smoke
    de test 2026-10-20 (negocio solvershub, cambiar de plan): la negación se tomaba como
    afirmación, el reintento la repetía y el turno salía con la respuesta neutral de ADR-0092
    («Dejame verificar eso y te confirmo»), sin que nadie verificara nada."""
    ran = {r.get("_tool_name") for r in (tool_results or [])}
    for label, (pattern, tools) in _SUSCRIPCION_CLAIMS.items():
        if afirma_alguna(response_text or "", pattern) and not (ran & tools):
            return GuardrailResult(
                passed=False,
                reason=f"Afirmación de suscripción '{label}' sin la tool correspondiente.",
                retry_instruction=(
                    f"Dijiste que la suscripción quedó '{label}' pero no llamaste la herramienta "
                    f"({', '.join(sorted(tools))}). Llamala ahora, o decí claramente que no se pudo."
                ),
            )
    return GuardrailResult(passed=True)

"""`cob_suscripcion_check` (pack-cobranza) solo mira lo que el texto AFIRMA (smoke de test
2026-10-20, negocio solvershub): «No encontré una suscripción activa» no es una suscripción
creada; antes salía la respuesta neutral de ADR-0092 sin que nadie verificara nada."""

from __future__ import annotations

import pytest
from pack_cobranza.guardrails import suscripcion_guardrail


@pytest.mark.parametrize(
    "texto",
    [
        "No tenés una suscripción activa a tu nombre. ¿Quieres contratar un plan?",
        "No encontré una suscripción activa con este chat, así que no puedo cambiar tu plan.",
        "¿Quieres que active tu suscripción?",
        "Para activar tu suscripción necesito que elijas un plan.",
        "Todavía no quedó cancelada tu suscripción.",
    ],
)
async def test_negaciones_preguntas_y_condicionales_no_afirman(texto: str) -> None:
    assert (await suscripcion_guardrail(texto, [])).passed


@pytest.mark.parametrize(
    "texto",
    [
        "Listo, tu suscripción quedó activa desde hoy.",
        "Ya cancelé tu suscripción.",
        "Perfecto, pausé tu suscripción hasta el próximo mes.",
    ],
)
async def test_una_afirmacion_sin_la_tool_sigue_fallando(texto: str) -> None:
    assert not (await suscripcion_guardrail(texto, [])).passed


async def test_con_la_tool_del_turno_pasa() -> None:
    resultado = {"_tool_name": "cob_cancelar_suscripcion", "_current_turn": True, "_ok": True}
    assert (await suscripcion_guardrail("Ya cancelé tu suscripción.", [resultado])).passed

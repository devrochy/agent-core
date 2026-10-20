"""Un traspaso a una persona se afirma con `escalate_to_human` del turno (smoke de test 2026-10-20:
con la escalación anterior ya resuelta, el modelo dijo «ya le pasé tu caso a una persona del
equipo» y nadie lo iba a atender)."""

from __future__ import annotations

import pytest
from pack_commerce.module import COMPLETION_CLAIMS

from core.mcp.types import Afirmacion
from core.orchestrator.verificador import Evidencia, verificar

PATRON, TOOLS = COMPLETION_CLAIMS["asesor_humano"]
AFIRMACION = [Afirmacion(label="asesor_humano", patron=PATRON, tools=frozenset(TOOLS))]
ESCALO = {"_tool_name": "escalate_to_human", "_ok": True, "_current_turn": True, "status": "escalated"}


@pytest.mark.parametrize(
    "texto",
    [
        "Entiendo, Robinson. Ya le pasé tu caso a una persona del equipo para que te confirme la demo.",
        "Listo, ya te comunico con una persona de nuestro equipo.",
        "Ya escalé tu caso.",
        "Una persona del equipo te va a contactar por este chat.",
    ],
)
def test_sin_la_tool_del_turno_falla_y_con_ella_pasa(texto: str) -> None:
    assert verificar(texto, Evidencia.desde([]), AFIRMACION).motivo == "status_sin_evidencia"
    assert verificar(texto, Evidencia.desde([ESCALO]), AFIRMACION).passed


@pytest.mark.parametrize(
    "texto",
    [
        "Si quieres, te comunico con una persona del equipo para revisarlo.",
        "¿Quieres que te pase con una persona del equipo?",
        "No te puedo pasar con una persona ahora, pero sigo ayudándote.",
        "Eso lo maneja una persona de nuestro equipo.",
    ],
)
def test_ofrecer_o_negar_no_afirma(texto: str) -> None:
    assert verificar(texto, Evidencia.desde([]), AFIRMACION).passed

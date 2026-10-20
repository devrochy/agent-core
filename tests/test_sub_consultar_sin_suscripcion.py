"""Sin suscripción ligada a la conversación (smoke de test 2026-10-20), `sub_consultar` ofrece las
dos salidas: una persona que verifique la identidad o contratar. Nunca busca por correo."""

from __future__ import annotations

from core.subscriptions.tools import _view_como_resultado


def test_sin_suscripcion_ofrece_persona_o_contratar() -> None:
    resultado = _view_como_resultado(None)
    assert resultado["encontrado"] is False
    assert "escalate_to_human" in resultado["hint"] and "contratar un plan" in resultado["hint"]


def test_con_suscripcion_no_cambia() -> None:
    assert _view_como_resultado({"plan": "Basic"}) == {"encontrado": True, "plan": "Basic"}  # type: ignore[arg-type]

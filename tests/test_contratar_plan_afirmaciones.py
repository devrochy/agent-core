"""`pack_subscriptions.contratar_plan`: no se puede decir que la inscripción quedó registrada sin que
`sub_inscribir` lo respalde (antes lo cubría `pack_commerce.contratar_plan` con `crear_pedido`;
medición pass^4 del 2026-10-24: el modelo dijo «ya quedó registrada» sin llamar a la tool).
SPEC-43 (HU-COM-17) movió el procedimiento a Suscripciones."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.kernel.procedures import compilar_afirmaciones, validar
from core.orchestrator.verificador.evidencia import Evidencia
from core.orchestrator.verificador.verificador import verificar

PROCEDIMIENTO = Path(__file__).parents[1] / "packages/pack-subscriptions/procedures/contratar_plan.json"


@pytest.mark.parametrize(
    "borrador",
    [
        "Gracias, Ana. Ya quedaste inscrito en el Plan Premium anual.",
        "Tu inscripción creada. En un momento te comparto los datos de la cuenta.",
        "Listo, quedó registrada tu inscripción.",
        "Perfecto, suscripción creada.",
    ],
)
def test_contratar_plan_no_deja_decir_que_quedo_inscrito_sin_la_tool(borrador: str) -> None:
    proc, _ = validar(json.loads(PROCEDIMIENTO.read_text("utf-8")))
    assert proc is not None and proc.id == "pack_subscriptions.contratar_plan"
    afirmaciones = compilar_afirmaciones(proc)
    [estado] = [a for a in afirmaciones if "sub_inscribir" in a.tools]
    sin = verificar(borrador, Evidencia.desde([]), afirmaciones)
    assert not sin.passed and sin.motivo == "status_sin_evidencia", borrador
    con = verificar(borrador, Evidencia.desde([{"_tool_name": "sub_inscribir", "_ok": True}]), [estado])
    assert con.passed

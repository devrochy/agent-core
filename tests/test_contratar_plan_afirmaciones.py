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


async def test_el_resumen_nombra_el_plan_como_el_dato_de_la_tarea(monkeypatch: pytest.MonkeyPatch) -> None:
    """La línea del plan se llama `plan_code`, como el dato de la tarea: el kernel no la repite con el SKU entre
    los datos del cliente (prueba de contratación en test, 2026-11-15: salían «Plan: Free mensual» y «Plan: PLAN-FREE»)."""
    import uuid
    from types import SimpleNamespace

    from pack_subscriptions.procedimientos import resumen_de_inscripcion

    import core.provisioning.antiabuso as antiabuso
    import core.subscriptions.tools as tools_mod

    async def cotizacion(ctx, plan_code, codigo_promocion=None):  # type: ignore[no-untyped-def]
        return {"plan": "Plan Free", "periodicidad": "mensual", "descuento": 0, "total_primer_cargo": 0.0}

    async def sin_terminos(conversation_id, *, anotar):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(tools_mod, "cotizacion_de_inscripcion", cotizacion)
    monkeypatch.setattr(antiabuso, "terminos_del_resumen", sin_terminos)
    datos = await resumen_de_inscripcion({"plan_code": "PLAN-FREE"}, SimpleNamespace(conversation_id=uuid.uuid4()))
    assert datos is not None
    assert [d.nombre for d in datos] == ["plan_code", "total del primer cargo"]
    assert datos[0].valor == "Free mensual"

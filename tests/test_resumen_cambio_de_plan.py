"""Resumen del cambio de plan (`sub_aplicar_cambio_plan`): sale de la cotización vigente del servidor, no de
los argumentos del modelo. Antes el kernel mostraba los argumentos crudos («Plan: PLAN-PREMIUM» y el monto con el
rótulo «Total del primer cargo»), visto en los journeys con LLM real del 2026-11-18."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.kernel.confirmacion import Dato, armar
from core.kernel.procedures.clasificador import es_confirmacion
from core.subscriptions import SubscriptionError

SUBIR = {
    "plan_code": "PLAN-PREMIUM", "plan_actual": "Plan Basic", "plan_nuevo": "Plan Premium", "cambio": "pendiente_pago",
    "monto_a_pagar": 400000.0, "precio_nuevo": 600000.0, "moneda": "COP",
}
BAJAR = {
    "plan_code": "PLAN-BASIC", "plan_actual": "Plan Premium", "plan_nuevo": "Plan Basic", "cambio": "programado",
    "monto_a_pagar": 0.0, "precio_nuevo": 200000.0, "moneda": "COP",
}


def _cotizacion(monkeypatch: pytest.MonkeyPatch, cot: dict[str, Any] | Exception) -> None:
    import core.subscriptions.tools as tools_mod

    async def cotizacion(ctx, plan_code, codigo_promocion=None):  # type: ignore[no-untyped-def]
        if isinstance(cot, Exception):
            raise cot
        return cot

    monkeypatch.setattr(tools_mod, "cotizacion_del_cliente", cotizacion)


async def test_subir_de_plan_nombra_los_planes_y_lo_que_paga_hoy(monkeypatch: pytest.MonkeyPatch) -> None:
    from pack_subscriptions.procedimientos import resumen_de_cambio_de_plan

    _cotizacion(monkeypatch, SUBIR)
    datos = await resumen_de_cambio_de_plan({"plan_code": "PLAN-PREMIUM", "monto_a_pagar": 400000}, SimpleNamespace())
    assert datos is not None
    assert [(d.nombre, d.valor, d.es_monto) for d in datos] == [
        ("cambio de plan", "del Plan Basic al Plan Premium", False),
        ("a pagar hoy", 400000.0, True),
        ("precio nuevo", 600000.0, True),
        ("el cambio aplica", "al pagar la diferencia", False),
    ]


async def test_bajar_de_plan_dice_que_aplica_el_proximo_periodo(monkeypatch: pytest.MonkeyPatch) -> None:
    from pack_subscriptions.procedimientos import resumen_de_cambio_de_plan

    _cotizacion(monkeypatch, BAJAR)
    datos = await resumen_de_cambio_de_plan({"plan_code": "PLAN-BASIC", "monto_a_pagar": 0}, SimpleNamespace())
    assert datos is not None
    assert datos[-1].valor == "desde tu próximo período"
    texto = armar("Cambiar de plan", [Dato(d.nombre, d.valor, es_monto=d.es_monto) for d in datos], "x", completo=True).texto
    assert "PLAN-BASIC" not in texto and "primer cargo" not in texto
    # El cliente confirma nombrando el plan que vio; otro plan no confirma.
    assert es_confirmacion("Sí, confirmo el Plan Basic.", eco=texto)
    assert not es_confirmacion("Sí, confirmo el Plan Gold.", eco=texto)


@pytest.mark.parametrize("monto", [399999, None, "abc"])
async def test_sin_resumen_si_el_monto_no_es_el_de_la_cotizacion(monkeypatch: pytest.MonkeyPatch, monto: Any) -> None:
    from pack_subscriptions.procedimientos import resumen_de_cambio_de_plan

    _cotizacion(monkeypatch, SUBIR)
    assert await resumen_de_cambio_de_plan({"plan_code": "PLAN-PREMIUM", "monto_a_pagar": monto}, SimpleNamespace()) is None


async def test_sin_resumen_si_el_plan_ya_no_vale(monkeypatch: pytest.MonkeyPatch) -> None:
    from pack_subscriptions.procedimientos import resumen_de_cambio_de_plan

    _cotizacion(monkeypatch, SubscriptionError("No existe ese plan.", code="plan_no_encontrado"))
    assert await resumen_de_cambio_de_plan({"plan_code": "PLAN-X", "monto_a_pagar": 0}, SimpleNamespace()) is None

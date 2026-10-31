"""D-16 sobre el catálogo de planes de Suscripciones (SPEC-43; antes HU-SUB-06 en commerce).

Reglas puras, sin base: solo un plan de $0 (Free) tiene prueba; en un plan de pago no se admite. Y
el estado de pago con el que nace un pedido de Comercio, que ya no distingue planes (HU-COM-17).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pack_commerce.skills import _estado_pago_inicial
from pack_subscriptions.planes import DatosDePlan, PlanError, validar


def _plan(precio: object, trial_dias: int) -> DatosDePlan:
    return DatosDePlan(codigo="PLAN-X", nombre="X", precio=Decimal(str(precio)), periodicidad="mensual", trial_dias=trial_dias)


def test_free_conserva_la_prueba() -> None:
    plan = validar(_plan(0, 30))
    assert plan.trial_dias == 30 and plan.precio == Decimal("0.00")


def test_plan_pago_con_prueba_se_rechaza() -> None:
    with pytest.raises(PlanError) as exc:
        validar(_plan(99000, 30))
    assert exc.value.code == "prueba_en_plan_de_pago"


def test_plan_pago_sin_prueba_es_valido() -> None:
    assert validar(_plan(99000, 0)).trial_dias == 0


@pytest.mark.parametrize(
    ("metodo", "esperado"),
    [
        ("efectivo_contraentrega", "pagado"),  # un producto contra entrega se da por pagado al entregar
        ("transferencia", "pendiente"),
        ("pago_en_linea", "pendiente"),
    ],
)
def test_estado_pago_inicial_de_un_pedido(metodo: str, esperado: str) -> None:
    assert _estado_pago_inicial(metodo) == esperado

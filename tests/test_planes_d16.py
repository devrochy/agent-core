"""HU-SUB-06 · reglas puras de D-16 en commerce (sin BD)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pack_commerce.planes import estado_pago_inicial, snapshot_de_plan


@pytest.mark.parametrize(
    ("metodo", "es_plan", "total", "esperado"),
    [
        ("efectivo_contraentrega", True, 0, "pagado"),  # Free: nada que cobrar
        ("pago_en_linea", True, Decimal("0"), "pagado"),
        ("efectivo_contraentrega", True, 99000, "pendiente"),  # plan contra entrega espera el pago
        ("pago_en_linea", True, 99000, "pendiente"),
        ("transferencia", False, 50000, "pendiente"),
        ("efectivo_contraentrega", False, 50000, "pagado"),  # producto: igual que antes
    ],
)
def test_estado_pago_inicial(metodo, es_plan, total, esperado) -> None:
    assert estado_pago_inicial(metodo, es_plan, total) == esperado


def test_free_conserva_la_prueba() -> None:
    plan, ignorado = snapshot_de_plan(
        sku="PLAN-FREE", nombre="Free", precio=0, periodicidad="mensual", trial_days=30
    )
    assert plan["trial_dias"] == 30
    assert ignorado is False


def test_plan_pago_ignora_la_prueba_y_lo_reporta() -> None:
    plan, ignorado = snapshot_de_plan(
        sku="PLAN-BASIC", nombre="Basic", precio=Decimal("99000"), periodicidad="mensual", trial_days=30
    )
    assert plan["trial_dias"] == 0
    assert plan["precio"] == 99000.0
    assert ignorado is True


def test_plan_pago_sin_prueba_no_reporta() -> None:
    _, ignorado = snapshot_de_plan(
        sku="PLAN-BASIC", nombre="Basic", precio=99000, periodicidad="mensual", trial_days=None
    )
    assert ignorado is False

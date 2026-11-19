"""El resumen de confirmación no repite la periodicidad que el nombre del plan ya dice (corrida de pass^4 del
2026-11-04: «Premium anual anual»). Sin BD ni red."""

from __future__ import annotations

import pytest
from pack_subscriptions.procedimientos import _nombre_de_plan


@pytest.mark.parametrize(
    ("nombre", "periodicidad", "esperado"),
    [
        ("Plan Premium", "mensual", "Premium mensual"),
        ("Plan Premium anual", "anual", "Premium anual"),
        ("Plan Basic", None, "Basic"),
        ("Premium", "anual", "Premium anual"),
        ("Plan Premium ANUAL", "anual", "Premium ANUAL"),
    ],
)
def test_no_repite_la_periodicidad(nombre: str, periodicidad: str | None, esperado: str) -> None:
    assert _nombre_de_plan(nombre, periodicidad) == esperado

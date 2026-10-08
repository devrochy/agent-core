"""HU-SUB-07 · prorrateo del upgrade por días de calendario."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pack_subscriptions.prorrateo import diferencia_prorrateada

INICIO = datetime(2026, 10, 1, 9, tzinfo=UTC)
FIN = datetime(2026, 10, 31, 9, tzinfo=UTC)  # 30 días


def test_mitad_de_periodo_cobra_la_mitad_de_la_diferencia() -> None:
    ahora = datetime(2026, 10, 16, 15, tzinfo=UTC)  # faltan 15 días
    assert diferencia_prorrateada(Decimal(200000), Decimal(600000), INICIO, FIN, ahora) == Decimal(200000)


def test_un_dia_antes_del_corte() -> None:
    ahora = datetime(2026, 10, 30, 10, tzinfo=UTC)
    assert diferencia_prorrateada(Decimal(200000), Decimal(600000), INICIO, FIN, ahora) == Decimal("13333")


def test_downgrade_y_mismo_precio_no_cobran() -> None:
    ahora = datetime(2026, 10, 16, tzinfo=UTC)
    assert diferencia_prorrateada(Decimal(600000), Decimal(200000), INICIO, FIN, ahora) == 0
    assert diferencia_prorrateada(Decimal(200000), Decimal(200000), INICIO, FIN, ahora) == 0


def test_el_dia_del_corte_no_cobra_y_otras_monedas_van_al_centavo() -> None:
    assert diferencia_prorrateada(Decimal(10), Decimal(20), INICIO, FIN, FIN) == 0
    ahora = datetime(2026, 10, 21, tzinfo=UTC)  # faltan 10 de 30
    assert diferencia_prorrateada(Decimal(10), Decimal(20), INICIO, FIN, ahora, moneda="USD") == Decimal("3.33")

"""Cartera recurrente / suscripción por cliente (ADR-0082).

Unit: `fecha_cargo` (determinista). Integración: el cron genera el cargo del
período y avanza `proximo_cargo`; las tools crean/consultan la suscripción.
"""

from __future__ import annotations

from datetime import date

from pack_cobranza.jobs import fecha_cargo


def test_fecha_cargo_mismo_mes_no_estricto() -> None:
    assert fecha_cargo(15, date(2026, 3, 10), estricto=False) == date(2026, 3, 15)


def test_fecha_cargo_estricto_mismo_dia_avanza_un_mes() -> None:
    assert fecha_cargo(15, date(2026, 3, 15), estricto=True) == date(2026, 4, 15)


def test_fecha_cargo_dia_ya_paso_pasa_al_siguiente_mes() -> None:
    assert fecha_cargo(5, date(2026, 3, 10), estricto=False) == date(2026, 4, 5)


def test_fecha_cargo_clampea_dia_a_28() -> None:
    assert fecha_cargo(31, date(2026, 3, 10), estricto=False) == date(2026, 3, 28)


def test_fecha_cargo_rota_el_anio() -> None:
    assert fecha_cargo(10, date(2026, 12, 20), estricto=False) == date(2027, 1, 10)

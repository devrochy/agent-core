"""HU-SUB-04 · Gherkin «Período real»: períodos por calendario, no +30 días."""

from __future__ import annotations

from datetime import UTC, datetime

from pack_subscriptions.periodos import etiqueta_periodo, sumar_periodo


def d(y: int, m: int, day: int) -> datetime:
    return datetime(y, m, day, 9, 0, tzinfo=UTC)


def test_anual_mismo_dia_del_anio_siguiente() -> None:
    assert sumar_periodo(d(2026, 10, 15), "anual") == d(2027, 10, 15)


def test_mensual_del_31_de_enero_vence_el_ultimo_dia_de_febrero_y_vuelve_al_31() -> None:
    feb = sumar_periodo(d(2027, 1, 31), "mensual", 31)
    assert feb == d(2027, 2, 28)
    assert sumar_periodo(feb, "mensual", 31) == d(2027, 3, 31)
    assert sumar_periodo(d(2028, 1, 31), "mensual", 31) == d(2028, 2, 29)  # bisiesto


def test_trimestral_suma_tres_meses() -> None:
    assert sumar_periodo(d(2026, 11, 30), "trimestral", 30) == d(2027, 2, 28)
    assert sumar_periodo(d(2026, 10, 15), "trimestral") == d(2027, 1, 15)


def test_anual_desde_29_de_febrero() -> None:
    assert sumar_periodo(d(2028, 2, 29), "anual", 29) == d(2029, 2, 28)


def test_periodicidad_desconocida_es_mensual() -> None:
    assert sumar_periodo(d(2026, 10, 15), "quincenal") == d(2026, 11, 15)


def test_etiqueta_del_periodo() -> None:
    assert etiqueta_periodo(d(2026, 10, 15), "mensual") == "2026-10"
    assert etiqueta_periodo(d(2026, 10, 15), "anual") == "2026"

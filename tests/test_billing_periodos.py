"""HU-SH-04 (ADR-0123): el período de la plataforma y su referencia, sin BD.

La referencia `plat-<slug>-<AAAAMM>` es la misma en los dos canales (asistente y panel):
por eso los períodos se cortan por calendario, igual que en `pack_subscriptions`, y dos
períodos seguidos nunca comparten el mes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.billing.periodos import (
    etiqueta_de_referencia,
    periodo_a_cobrar,
    periodo_de,
    referencia_del_periodo,
)


def d(y: int, m: int, day: int, h: int = 9) -> datetime:
    return datetime(y, m, day, h, 0, tzinfo=UTC)


def test_referencia_del_periodo_es_la_del_gherkin() -> None:
    assert referencia_del_periodo("acme", d(2026, 11, 9)) == "plat-acme-202611"


def test_la_etiqueta_se_lee_de_la_referencia_aunque_el_slug_tenga_guiones() -> None:
    assert etiqueta_de_referencia("plat-pizzeria-roma-3f2a1c-202612") == "2026-12"
    assert etiqueta_de_referencia("sub-1234") is None
    assert etiqueta_de_referencia("plat-acme-2026") is None


def test_el_periodo_mensual_respeta_el_dia_ancla() -> None:
    feb = periodo_de("acme", d(2027, 1, 31), dia_ancla=31)
    assert (feb.inicio, feb.fin, feb.referencia) == (d(2027, 1, 31), d(2027, 2, 28), "plat-acme-202701")
    mar = periodo_de("acme", feb.fin, dia_ancla=31)
    assert mar.fin == d(2027, 3, 31)


def test_dos_periodos_seguidos_nunca_comparten_referencia() -> None:
    """Con +30 días, un período del 1 de enero y el siguiente (31 de enero) caían en el mismo mes."""
    inicio = d(2027, 1, 31)
    vistas: set[str] = set()
    for _ in range(36):
        periodo = periodo_de("acme", inicio, dia_ancla=31)
        assert periodo.referencia not in vistas
        vistas.add(periodo.referencia)
        inicio = periodo.fin
    inicio = d(2027, 1, 1)
    assert periodo_de("acme", inicio).referencia != periodo_de("acme", periodo_de("acme", inicio).fin).referencia


def test_sin_plan_pago_el_periodo_arranca_hoy() -> None:
    """Free (en prueba o vencida) → el plan pago arranca hoy, sin sumar la prueba (D-16)."""
    ahora = d(2026, 10, 9)
    periodo = periodo_a_cobrar(
        slug="acme", plan_id="free", status="trialing", period_end=ahora + timedelta(days=20), dia_ancla=None, now=ahora
    )
    assert (periodo.inicio, periodo.fin, periodo.desde_cero) == (ahora, d(2026, 11, 9), True)
    assert periodo.referencia == "plat-acme-202610"


def test_con_el_periodo_al_dia_se_cobra_el_siguiente() -> None:
    ahora = d(2026, 11, 4)
    periodo = periodo_a_cobrar(
        slug="acme", plan_id="premium", status="active", period_end=d(2026, 11, 9), dia_ancla=9, now=ahora
    )
    assert (periodo.inicio, periodo.fin, periodo.referencia) == (d(2026, 11, 9), d(2026, 12, 9), "plat-acme-202611")
    assert periodo.desde_cero is False


def test_vencido_hace_poco_se_cobra_el_periodo_que_ya_corre() -> None:
    ahora = d(2026, 11, 12)
    periodo = periodo_a_cobrar(
        slug="acme", plan_id="basic", status="past_due", period_end=d(2026, 11, 9), dia_ancla=9, now=ahora
    )
    assert (periodo.inicio, periodo.fin) == (d(2026, 11, 9), d(2026, 12, 9))


def test_vencido_hace_mas_de_un_periodo_arranca_uno_nuevo_hoy() -> None:
    """Pagar un período que ya terminó no desbloquearía el negocio."""
    ahora = d(2027, 1, 20)
    periodo = periodo_a_cobrar(
        slug="acme", plan_id="basic", status="past_due", period_end=d(2026, 11, 9), dia_ancla=9, now=ahora
    )
    assert (periodo.inicio, periodo.desde_cero) == (ahora, True)
    assert periodo.fin == d(2027, 2, 20)


def test_sin_dia_ancla_usa_el_dia_del_fin_del_periodo() -> None:
    periodo = periodo_a_cobrar(
        slug="acme", plan_id="basic", status="active", period_end=d(2026, 11, 30), dia_ancla=None, now=d(2026, 11, 28)
    )
    assert periodo.fin == d(2026, 12, 30)

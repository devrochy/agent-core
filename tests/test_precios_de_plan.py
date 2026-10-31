"""HU-SH-01 · el precio de un plan de la plataforma sale de core/billing/plans.py.

Desde SPEC-43 el cálculo vive en Suscripciones (`pack_subscriptions.precios_de_plan`)."""

from __future__ import annotations

from decimal import Decimal

from pack_subscriptions.precios_de_plan import precio_de_plataforma

from core.billing.plans import get_plan


def test_toma_el_precio_del_plan_de_la_plataforma() -> None:
    basic = get_plan("basic")
    assert basic is not None
    assert precio_de_plataforma("basic") == Decimal(basic.price_cop)


def test_si_cambia_el_plan_cambia_el_precio(monkeypatch) -> None:
    import core.billing.plans as plans

    basic = plans.get_plan("basic")
    assert basic is not None
    monkeypatch.setitem(plans.PLANS_BY_ID, "basic", basic.__class__(**{**basic.__dict__, "price_cop": 250_000}))
    assert precio_de_plataforma("basic") == Decimal(250_000)


def test_sin_referencia_o_plan_desconocido_no_toca_el_precio() -> None:
    assert precio_de_plataforma(None) is None
    assert precio_de_plataforma("") is None
    assert precio_de_plataforma("no-existe") is None

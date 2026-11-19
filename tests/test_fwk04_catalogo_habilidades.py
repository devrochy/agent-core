"""HU-FWK-04: el catálogo de habilidades expone metadatos por pack.

Se importan las clases de módulo directamente (no dependen del registro en
proceso ni de la BD).
"""

from __future__ import annotations


def _clases():
    from pack_calendar.module import GoogleCalendarModule
    from pack_cobranza.module import CobranzaModule
    from pack_commerce.module import CRMModule
    from pack_payments.module import PaymentsModule
    from pack_subscriptions.module import SubscriptionsModule

    return [CRMModule, GoogleCalendarModule, PaymentsModule, SubscriptionsModule, CobranzaModule]


def test_packs_declaran_metadatos_de_catalogo() -> None:
    for cls in _clases():
        assert cls.display_name, f"{cls.id}: falta display_name"
        assert cls.category, f"{cls.id}: falta category"
        assert cls.action_summary, f"{cls.id}: falta action_summary"
        assert cls.min_plan in ("free", "basic", "premium", "empresarial")


def test_commerce_metadatos() -> None:
    from pack_commerce.module import CRMModule

    assert CRMModule.display_name == "Comercio"
    assert CRMModule.category == "Ventas"
    assert "Cotizar" in CRMModule.action_summary

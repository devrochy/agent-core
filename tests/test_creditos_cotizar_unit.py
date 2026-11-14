"""HU-BILL-16 · cotizar acciones de IA y el 409 de crédito insuficiente (ADR-0194, ADR-0142 §6).

Sin base: la cotización es aritmética del tarifario y el 409 sale de `plan_limit_error`.
"""

from __future__ import annotations

import pytest

from core.api.plan_limits import plan_limit_error
from core.billing.creditos import cotizar
from core.billing.enforcement import QuotaExceeded
from core.config import get_settings


@pytest.fixture
def tarifa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "billing_trm_cop", 4000.0)
    monkeypatch.setattr(get_settings(), "billing_vision_costo_referencia_usd", 0.003)


def test_con_llave_propia_solo_se_cobra_el_fee(tarifa: None) -> None:
    assert cotizar("vision", 1, "byok") == 1
    assert cotizar("vision", 7, "byok") == 7
    assert cotizar("vision", 0, "byok") == 0


def test_con_la_llave_de_la_plataforma_costo_de_referencia_mas_margen(tarifa: None) -> None:
    # 0,003 USD × 4000 × 3 / 10 = 3,6 + fee 1,0 = 4,6 por foto.
    assert cotizar("vision", 1, "platform") == 5  # hacia arriba: nunca por debajo
    assert cotizar("vision", 10, "platform") == 46


def test_el_costo_de_referencia_es_configurable(monkeypatch: pytest.MonkeyPatch, tarifa: None) -> None:
    monkeypatch.setattr(get_settings(), "billing_vision_costo_referencia_usd", 0.0)
    assert cotizar("vision", 3, "platform") == 3  # sin costo variable queda el fee


def test_solo_se_cotizan_los_conceptos_con_referencia(tarifa: None) -> None:
    with pytest.raises(ValueError):
        cotizar("llm_turn", 1, "platform")
    with pytest.raises(ValueError):
        cotizar("vision", -1, "byok")


def test_credito_insuficiente_es_409_plan_limit_creditos() -> None:
    error = plan_limit_error(QuotaExceeded("creditos", 2499, 2500))
    assert error.status_code == 409
    assert error.code == "plan_limit_creditos"
    assert error.details == {"metric": "creditos", "used": 2499, "limit": 2500}
    assert "2500" in error.message and "No se cobró nada" in error.message


def test_el_bloqueo_viene_encendido_por_defecto() -> None:
    from core.config import Settings

    assert Settings.model_fields["billing_bloquear_por_creditos"].default is True

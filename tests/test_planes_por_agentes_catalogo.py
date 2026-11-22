"""HU-BILL-05 · métricas por agentes y contrato del 409 (D-35, ADR-0142). Sin base de datos."""

from __future__ import annotations

import pytest


def test_metricas_nuevas_registradas() -> None:
    from core.billing.metrics import METRIC_KEYS, get_metric

    assert {"agentes", "colaboradores"} <= METRIC_KEYS
    # `plugins` salió del registro con los planes v3 (ADR-0208 §1); el catálogo v3 está en
    # `tests/test_planes_v3_catalogo.py`.
    assert "plugins" not in METRIC_KEYS
    agentes = get_metric("agentes")
    assert agentes is not None and agentes.scope == "lifetime"


def test_el_credito_se_espeja_en_features_sin_tocar_el_catalogo() -> None:
    from core.billing.plans import PLANS_BY_ID, _features_espejadas

    plan = PLANS_BY_ID["basic"]
    espejo = _features_espejadas(plan)
    assert espejo["credito_mensual"] == 12_000
    assert "credito_mensual" not in plan.features


@pytest.mark.parametrize(
    ("metric", "codigo", "texto"),
    [("agentes", "plan_limit_agentes", "agente"), ("colaboradores", "plan_limit_colaboradores", "colaborador")],
)
def test_error_de_limite_de_plan(metric: str, codigo: str, texto: str) -> None:
    from core.api.plan_limits import plan_limit_error
    from core.billing.enforcement import QuotaExceeded

    err = plan_limit_error(QuotaExceeded(metric, 2, 2))
    assert (err.status_code, err.code) == (409, codigo)
    assert err.details == {"metric": metric, "used": 2, "limit": 2}
    assert texto in err.message and "mejora tu plan" in err.message.lower()


async def test_enforce_growth_solo_acepta_metricas_estructurales() -> None:
    from core.billing.enforcement import enforce_growth

    with pytest.raises(ValueError):
        await enforce_growth(__import__("uuid").uuid4(), "mensajes", session=object())

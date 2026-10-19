"""HU-BILL-05 · catálogo de planes por agentes y contrato del 409 (D-35, ADR-0142). Sin base de datos."""

from __future__ import annotations

import pytest


def test_catalogo_v2_con_las_cifras_del_modelo_de_precios() -> None:
    from core.billing.plans import PLANS_BY_ID

    # modelo-de-precios.md §5: agentes, colaboradores y crédito mensual por plan.
    esperado = {
        "free": (1, 2, 2_500),
        "basic": (2, 5, 20_000),
        "premium": (5, 15, 75_000),
        "empresarial": (None, 50, 240_000),
    }
    for plan_id, (agentes, colaboradores, credito) in esperado.items():
        plan = PLANS_BY_ID[plan_id]
        assert plan.limits["agentes"] == agentes
        assert plan.limits["colaboradores"] == colaboradores
        assert plan.monthly_credits == credito
        # «Habilidades activas» deja de ser tope (la métrica sigue definida para lecturas).
        assert "plugins" not in plan.limits


def test_metricas_nuevas_registradas() -> None:
    from core.billing.metrics import METRIC_KEYS, get_metric

    assert {"agentes", "colaboradores", "plugins"} <= METRIC_KEYS
    agentes = get_metric("agentes")
    assert agentes is not None and agentes.scope == "lifetime"


def test_el_credito_se_espeja_en_features_sin_tocar_el_catalogo() -> None:
    from core.billing.plans import PLANS_BY_ID, _features_espejadas

    plan = PLANS_BY_ID["basic"]
    espejo = _features_espejadas(plan)
    assert espejo["credito_mensual"] == 20_000
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

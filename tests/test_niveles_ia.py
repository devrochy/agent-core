"""HU-BILL-24 · mapa de niveles de IA y techo del plan (ADR-0210), sin base ni red.

- El mapa fija `primary` y `router` por nivel; `deepseek-v4-pro` tiene precio en el proveedor.
- Premium y Empresarial incluyen Avanzado; Free y Basic no.
- El techo recorta lo que pertenece a un nivel superior (`primary`, `router`, `fallback`, `highRiskStage.model`) y
  deja pasar lo que el mapa no conoce; sin techo (llave propia o sin plan) no recorta nada.
- El router de dificultad en Avanzado usa v4-pro; con el techo de Estándar, flash.
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from agentcore_contracts import KernelSpec

from core.billing.plans import get_plan
from core.kernel.politica_modelo import decidir
from core.llm import niveles
from core.llm.difficulty_router import route_model
from core.llm.openai_compatible import PRICING
from core.orchestrator import loop

FLASH, PRO = "deepseek-flash", "deepseek-v4-pro"


def test_mapa_de_niveles() -> None:
    assert niveles.NIVELES == ("estandar", "avanzado")
    assert (niveles.modelos_de("estandar").primary, niveles.modelos_de("estandar").router) == (FLASH, FLASH)
    assert (niveles.modelos_de("avanzado").primary, niveles.modelos_de("avanzado").router) == (PRO, PRO)
    # Un valor raro nunca sube el costo.
    assert niveles.modelos_de("ultra").primary == FLASH and niveles.normalizar(None) == "estandar"
    assert niveles.VERSION_DEL_MAPA


def test_los_modelos_del_mapa_tienen_precio_en_el_proveedor() -> None:
    for nivel in niveles.NIVELES:
        m = niveles.modelos_de(nivel)
        assert m.primary in PRICING and m.router in PRICING
    assert PRICING[PRO][0] > PRICING[FLASH][0] and PRICING[PRO][1] > PRICING[FLASH][1]


@pytest.mark.parametrize(
    ("plan_id", "techo"), [("free", "estandar"), ("basic", "estandar"), ("premium", "avanzado"), ("empresarial", "avanzado")]
)
def test_nivel_que_incluye_cada_plan(plan_id: str, techo: str) -> None:
    plan = get_plan(plan_id)
    assert plan is not None
    assert niveles.nivel_del_plan(plan.features) == techo
    assert niveles.plan_incluye("estandar", plan.features)
    assert niveles.plan_incluye("avanzado", plan.features) is (techo == "avanzado")


def test_recortar_por_pertenencia_al_mapa() -> None:
    assert niveles.recortar(PRO, "estandar") == FLASH
    assert niveles.recortar(PRO, "estandar", "router") == FLASH
    assert niveles.recortar(PRO, "avanzado") == PRO
    assert niveles.recortar(FLASH, "avanzado") == FLASH
    assert niveles.recortar(PRO, None) == PRO  # llave propia o sin plan
    # Lo que el mapa no conoce no es «de un nivel»: el respaldo por defecto no se pierde.
    assert niveles.recortar("gpt-4o-mini", "estandar") == "gpt-4o-mini"
    assert niveles.nivel_de_modelo(PRO) == "avanzado" and niveles.nivel_de_modelo("otro") is None


def test_usa_llave_propia_pide_proveedor_y_llave() -> None:
    assert niveles.usa_llave_propia(types.SimpleNamespace(llm_provider="openai", llm_api_key_encrypted="x"))
    assert not niveles.usa_llave_propia(types.SimpleNamespace(llm_provider="openai", llm_api_key_encrypted=None))
    assert not niveles.usa_llave_propia(None)


def test_no_incluido_trae_el_codigo_y_los_detalles() -> None:
    exc = niveles.NivelNoIncluido("avanzado", "basic", "Basic")
    assert exc.code == "plan_nivel_ia_not_included"
    assert exc.details == {"nivel_ia": "avanzado", "plan": "basic"}
    assert "deepseek" not in str(exc).lower()


# ---------------------------------------------------------------- el turno
def _spec(primary: str, router: str | None = None, fallback: str | None = None) -> Any:
    return types.SimpleNamespace(model=types.SimpleNamespace(primary=primary, router=router, fallback=fallback))


def test_resolved_model_aplica_el_techo_sin_tocar_el_spec() -> None:
    spec = _spec(PRO, PRO)
    assert loop._resolved_model(None, spec, FLASH, techo="estandar") == FLASH
    assert loop._resolved_model(None, spec, FLASH, techo="avanzado") == PRO
    assert loop._resolved_model(None, spec, FLASH) == PRO
    assert spec.model.primary == PRO  # el spec está cacheado por versión: nunca se muta
    # El modelo fijado en el panel también queda bajo el techo.
    ajustes = types.SimpleNamespace(llm_model=PRO, llm_provider=None, llm_api_key_encrypted=None)
    assert loop._resolved_model(ajustes, spec, FLASH, techo="estandar") == FLASH


@pytest.mark.asyncio
async def test_el_techo_del_turno_no_aplica_con_llave_propia(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _techo(_tid: Any) -> str:
        return "estandar"

    monkeypatch.setattr(loop, "techo_del_negocio", _techo)
    monkeypatch.setattr(loop, "get_current_tenant_id", lambda: None)
    byok = types.SimpleNamespace(llm_provider="openai", llm_api_key_encrypted="x", llm_model=None)
    assert await loop._techo_del_turno(byok) is None
    assert await loop._techo_del_turno(None) == "estandar"


@pytest.mark.asyncio
async def test_el_techo_se_cierra_en_estandar_si_no_se_puede_leer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _falla(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("sin base")

    monkeypatch.setattr(niveles, "plan_del_negocio", _falla)
    assert await niveles.techo_del_negocio(None) == "estandar"  # type: ignore[arg-type]


@pytest.mark.parametrize(("techo", "esperado"), [("avanzado", PRO), ("estandar", FLASH), (None, PRO)])
def test_router_de_dificultad_con_el_router_del_nivel(techo: str | None, esperado: str) -> None:
    decision = route_model(
        message="¿Por qué me cobraron dos veces? Explícame la diferencia y compara con el mes pasado " * 4,
        turnos_sin_resolver=5,
        default_model=niveles.recortar(PRO, techo),
        reasoning_model=niveles.recortar(niveles.modelos_de("avanzado").router, techo, "router"),
        routing_enabled=True,
        requires_tools=False,
    )
    assert decision.reason == "complex_signals" and decision.model == esperado


def test_high_risk_stage_no_sube_por_encima_del_techo() -> None:
    politica = KernelSpec.model_validate(
        {"modelPolicy": {"highRiskStage": {"thinking": True, "model": PRO}}}
    ).modelPolicy
    assert decidir(True, politica, FLASH).model == PRO  # type: ignore[union-attr]
    recortada = decidir(True, politica, FLASH, techo="estandar")
    assert recortada is not None and recortada.model == FLASH and recortada.thinking
    sin_thinking = KernelSpec.model_validate(
        {"modelPolicy": {"highRiskStage": {"thinking": False, "model": PRO}}}
    ).modelPolicy
    assert decidir(True, sin_thinking, FLASH, techo="estandar") is None  # nada distinto que pedir
    assert decidir(True, politica, FLASH, techo="avanzado").model == PRO  # type: ignore[union-attr]


def test_la_politica_del_turno_recorta_fallback_y_high_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    pedidos: list[tuple[str, str]] = []

    def _primario(_ajustes: Any, model: str, thinking: bool | None = None) -> Any:
        pedidos.append(("alto", model))
        return types.SimpleNamespace(provider_name="deepseek")

    def _respaldo(_ajustes: Any, model: str) -> Any:
        pedidos.append(("respaldo", model))
        return types.SimpleNamespace(provider_name="deepseek")

    alto = KernelSpec.model_validate({"modelPolicy": {"highRiskStage": {"thinking": True, "model": PRO}}}).modelPolicy
    monkeypatch.setattr("core.kernel.politica_modelo.politica_por_defecto", lambda: alto)
    monkeypatch.setattr(loop, "_resolved_provider", _primario)
    monkeypatch.setattr(loop, "_resolved_provider_fallback", _respaldo)
    motor = types.SimpleNamespace(riesgo_alto=True)
    routing = types.SimpleNamespace(reason="default", model=FLASH)

    politica = loop._politica_del_turno(motor, None, _spec(FLASH, fallback=PRO), routing, techo="estandar")
    # El respaldo v4-pro baja a flash, que es el mismo modelo del turno: no hay respaldo distinto.
    assert politica is not None and not politica.hay_respaldo_declarado
    assert pedidos == [("alto", FLASH)]

    pedidos.clear()
    politica = loop._politica_del_turno(motor, None, _spec(FLASH, fallback=PRO), routing, techo="avanzado")
    assert politica is not None and politica.hay_respaldo_declarado
    assert pedidos == [("alto", PRO), ("respaldo", PRO)]

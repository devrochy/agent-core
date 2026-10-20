"""Pack de conocimiento, parte 1 (ADR-0145, HU-AOS-49): contrato 3.1.0 y topes por plan.

- `AgentSpecV3.knowledge` es opcional con default (un minor, adenda de ADR-0113 del
  2026-10-20); fuera de rango o con campos desconocidos no valida.
- Los topes `documentos` y `conocimiento_mb` están en el plan pero no son métricas del core:
  los cuenta el pack y los aplica `enforce_pack_growth` (lock antes de contar).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from agentcore_contracts import __version__
from agentcore_contracts.agentspec import AgentSpecV3, KnowledgeConfigV3
from pydantic import ValidationError

from core.billing import enforcement
from core.billing.enforcement import QuotaExceeded, enforce_pack_growth
from core.billing.metrics import METRIC_KEYS
from core.billing.plans import PACK_LIMIT_KEYS, PLANS_BY_ID, _validate_limits
from core.shared.tenant import tenant_scope

MODELO = {"primary": "deepseek-v4-flash"}
TENANT = uuid.UUID("20000000-0000-0000-0000-000000000049")


# ------------------------------------------------------------------ contrato
def test_version_minor_3_1_0() -> None:
    assert __version__ == "3.1.0"


def test_spec_sin_knowledge_sigue_validando() -> None:
    spec = AgentSpecV3.model_validate({"model": MODELO})
    assert spec.knowledge is None


def test_knowledge_por_defecto() -> None:
    spec = AgentSpecV3.model_validate({"model": MODELO, "knowledge": {}})
    # `minScore` None: el pack usa el umbral calibrado de cada modo (ADR-0145, parte 3).
    assert spec.knowledge == KnowledgeConfigV3(mode="hibrida", topK=4, minScore=None)


@pytest.mark.parametrize(
    "knowledge",
    [
        {"topK": 0},
        {"topK": 9},
        {"minScore": 1.5},
        {"mode": "vectorial"},
        {"topk": 3},
    ],
    ids=["topK_0", "topK_9", "minScore_alto", "modo_desconocido", "campo_desconocido"],
)
def test_knowledge_fuera_de_rango_no_valida(knowledge: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AgentSpecV3.model_validate({"model": MODELO, "knowledge": knowledge})


def test_knowledge_no_vive_en_memory() -> None:
    with pytest.raises(ValidationError):
        AgentSpecV3.model_validate({"model": MODELO, "memory": {"knowledge": {}}})


# ------------------------------------------------------------------ planes
def test_topes_del_conocimiento_por_plan() -> None:
    esperado = {"free": (3, 10), "basic": (20, 100), "premium": (100, 500), "empresarial": (500, 2_000)}
    for plan_id, (documentos, mb) in esperado.items():
        limites = PLANS_BY_ID[plan_id].limits
        assert (limites["documentos"], limites["conocimiento_mb"]) == (documentos, mb)


def test_topes_de_pack_no_son_metricas_del_core() -> None:
    # Ni barras de consumo ni conteo en `core/billing/metrics.py` (ADR-0145 §2).
    assert PACK_LIMIT_KEYS.isdisjoint(METRIC_KEYS)
    _validate_limits({"documentos": 1, "conocimiento_mb": 1})
    with pytest.raises(ValueError):
        _validate_limits({"documentoz": 1})


class _Sesion:
    def __init__(self, orden: list[str]) -> None:
        self.orden = orden

    async def execute(self, *_: Any, **__: Any) -> None:
        self.orden.append("lock")


@pytest.fixture
def plan(monkeypatch: pytest.MonkeyPatch):
    def _con(plan_id: str | None) -> None:
        async def _sub(_tenant: uuid.UUID, *, session: Any = None) -> Any:
            return SimpleNamespace(plan_id=plan_id) if plan_id else None

        monkeypatch.setattr(enforcement, "get_active_subscription", _sub)

    return _con


@pytest.fixture(autouse=True)
async def _en_el_scope():
    async with tenant_scope(TENANT):
        yield


def _contador(orden: list[str], valor: float):  # type: ignore[no-untyped-def]
    async def _contar(_s: Any) -> float:
        orden.append("contar")
        return valor

    return _contar


async def test_lock_antes_de_contar_y_pasa_con_cupo(plan) -> None:
    plan("free")
    orden: list[str] = []
    await enforce_pack_growth(TENANT, "documentos", _contador(orden, 2), session=_Sesion(orden))
    assert orden == ["lock", "contar"]


async def test_rechaza_el_documento_que_pasa_el_tope(plan) -> None:
    plan("free")
    orden: list[str] = []
    with pytest.raises(QuotaExceeded) as exc:
        await enforce_pack_growth(TENANT, "documentos", _contador(orden, 3), session=_Sesion(orden))
    assert (exc.value.metric, exc.value.used, exc.value.limit) == ("documentos", 3, 3)


async def test_mb_cuenta_el_incremento(plan) -> None:
    plan("free")
    orden: list[str] = []
    # 9,5 MB usados + 0,4 MB entra; + 0,6 MB pasa de 10.
    await enforce_pack_growth(
        TENANT, "conocimiento_mb", _contador(orden, 9.5), incremento=0.4, session=_Sesion(orden)
    )
    with pytest.raises(QuotaExceeded):
        await enforce_pack_growth(
            TENANT, "conocimiento_mb", _contador(orden, 9.5), incremento=0.6, session=_Sesion(orden)
        )


async def test_sin_suscripcion_no_hay_tope(plan) -> None:
    plan(None)
    orden: list[str] = []
    await enforce_pack_growth(TENANT, "documentos", _contador(orden, 10_000), session=_Sesion(orden))
    assert orden == ["lock"]


async def test_solo_topes_de_pack(plan) -> None:
    plan("free")
    with pytest.raises(ValueError):
        await enforce_pack_growth(TENANT, "agentes", _contador([], 0), session=_Sesion([]))


async def test_rechaza_un_tenant_distinto_del_scope(plan) -> None:
    plan("free")
    with pytest.raises(ValueError):
        await enforce_pack_growth(uuid.uuid4(), "documentos", _contador([], 0), session=_Sesion([]))

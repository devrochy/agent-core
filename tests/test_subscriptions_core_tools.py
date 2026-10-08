"""HU-SUB-01 / ADR-0106: las tools `sub_*` viven en el core y despachan al
proveedor del negocio; se ofrecen solo con un proveedor habilitado."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.subscriptions.tools as tools_mod
from core.subscriptions import subscription_registry

NOMBRES = {"sub_consultar", "sub_link_pago", "sub_pausar", "sub_cancelar", "sub_cambiar_plan"}


class _FakeCap:
    provider_id = "pack_subscriptions"

    def __init__(self) -> None:
        self.subscription_id = str(uuid.uuid4())

    async def list_for_customer(self, customer_id):
        return [
            {"subscription_id": self.subscription_id, "plan": "Basic", "estado": "activa"}
        ]

    async def charge_link(self, subscription_id, *, period=""):
        return {"url": "https://x", "referencia": "ref-1", "periodo": "2026-10", "monto": 100.0}

    async def pause(self, subscription_id, *, reason="", actor=""):
        return {"subscription_id": str(subscription_id), "estado": "pausada"}

    async def cancel(self, subscription_id, *, reason="", actor=""):
        return {"subscription_id": str(subscription_id), "estado": "cancelada"}

    async def change_plan(self, subscription_id, *, variant_id, plan):
        return {"subscription_id": str(subscription_id), "estado": "activa", "plan": plan["nombre"]}


@pytest.fixture()
def fake_cap(monkeypatch):
    cap = _FakeCap()
    subscription_registry.register("pack_subscriptions", cap)
    async def _cap_del_negocio(tenant_id):
        return cap
    monkeypatch.setattr(tools_mod, "capability_del_negocio", _cap_del_negocio)
    monkeypatch.setattr(tools_mod, "get_current_tenant_id", lambda: uuid.uuid4())
    yield cap
    subscription_registry.unregister("pack_subscriptions")


def _ctx(customer_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id or uuid.uuid4(),
        idempotency_key=str(uuid.uuid4()),
        channel="test",
    )


def test_las_cinco_tools_estan_en_el_core() -> None:
    skill = tools_mod.SubscriptionSkill()
    herramientas = {t.name for t in skill.get_tools()}
    assert herramientas == NOMBRES


@pytest.mark.asyncio
async def test_predicado_solo_con_proveedor_habilitado(monkeypatch, fake_cap) -> None:
    import core.agents.spec as spec_mod

    class _Spec:
        def __init__(self, packs: set[str]) -> None:
            self.packs = packs

    async def _spec_con(packs: set[str]) -> _Spec:
        return _Spec(packs)

    skill = tools_mod.SubscriptionSkill()
    herramientas = skill.get_tools()
    assert all(t.connection_predicate is not None for t in herramientas)

    monkeypatch.setattr(spec_mod, "load_active_spec", lambda: _spec_con({"pack_subscriptions"}))

    pred = herramientas[0].connection_predicate
    assert pred is not None and await pred(set()) is True

    monkeypatch.setattr(spec_mod, "load_active_spec", lambda: _spec_con(set()))
    assert await pred(set()) is False


@pytest.mark.asyncio
async def test_sub_consultar_despacha_y_envuelve_la_vista(fake_cap) -> None:
    resultado = await tools_mod.SubscriptionSkill().consultar({}, _ctx())
    assert resultado == {
        "encontrado": True,
        "subscription_id": fake_cap.subscription_id,
        "plan": "Basic",
        "estado": "activa",
    }


@pytest.mark.asyncio
async def test_sub_link_pago_despacha(fake_cap) -> None:
    resultado = await tools_mod.SubscriptionSkill().link_pago({}, _ctx())
    assert resultado["url"] == "https://x"
    assert resultado["referencia"] == "ref-1"
    # El core adjunta el link exacto aunque el modelo no lo copie (journeys HU-SUB-12).
    assert "https://x" in resultado["_safe_append"]
    assert "2026-10" in resultado["_safe_append"]


@pytest.mark.asyncio
async def test_sub_pausar_y_cancelar_despachan(fake_cap) -> None:
    skill = tools_mod.SubscriptionSkill()
    assert (await skill.pausar({}, _ctx()))["estado"] == "pausada"
    assert (await skill.cancelar({}, _ctx()))["estado"] == "cancelada"


class _CatalogoFalso:
    provider_id = "pack_commerce"

    async def plan_por_codigo(self, codigo):
        if codigo != "PLAN-PREMIUM":
            return None
        return "var-premium", {"plan_code": "PLAN-PREMIUM", "nombre": "Premium", "precio": 600000.0,
                               "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0}


@pytest.fixture()
def catalogo(monkeypatch):
    from core.subscriptions import plan_catalog_registry

    async def _para_negocio(_tid):
        return _CatalogoFalso()

    monkeypatch.setattr(plan_catalog_registry, "para_negocio", _para_negocio)


@pytest.mark.asyncio
async def test_sub_cambiar_plan_usa_el_precio_del_catalogo(fake_cap, catalogo) -> None:
    """HU-SUB-07 (ADR-0111): el precio que mande el LLM se ignora; manda el catálogo."""
    recibido = {}

    async def _change_plan(subscription_id, *, variant_id, plan):
        recibido.update(variant_id=variant_id, plan=plan)
        return {"subscription_id": str(subscription_id), "estado": "activa", "plan": "Basic",
                "cambio": "pendiente_pago",
                "cargo_cambio": {"monto": 200000.0, "link": "https://pagos/x", "referencia": "sub-1"}}

    fake_cap.change_plan = _change_plan
    resultado = await tools_mod.SubscriptionSkill().cambiar_plan(
        {"plan_code": "PLAN-PREMIUM", "precio": 0, "nombre": "Gratis"}, _ctx()
    )
    assert recibido["variant_id"] == "var-premium"
    assert recibido["plan"]["precio"] == 600000.0
    assert "https://pagos/x" in resultado["_safe_append"]


@pytest.mark.asyncio
async def test_sub_cambiar_plan_a_un_plan_que_no_existe(fake_cap, catalogo) -> None:
    from core.subscriptions import SubscriptionError

    with pytest.raises(SubscriptionError) as exc:
        await tools_mod.SubscriptionSkill().cambiar_plan({"plan_code": "PLAN-INVENTADO"}, _ctx())
    assert exc.value.code == "plan_no_encontrado"


@pytest.mark.asyncio
async def test_sub_cambiar_plan_sin_datos_es_error_controlado(fake_cap) -> None:
    from core.subscriptions import SubscriptionError

    with pytest.raises(SubscriptionError) as exc:
        await tools_mod.SubscriptionSkill().cambiar_plan(
            {"plan_code": ""}, _ctx()
        )
    assert exc.value.code == "plan_invalido"


@pytest.mark.asyncio
async def test_sin_proveedor_es_error_controlado(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from core.subscriptions import SubscriptionError

    monkeypatch.setattr(tools_mod, "capability_del_negocio", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_mod, "get_current_tenant_id", lambda: uuid.uuid4())
    with pytest.raises(SubscriptionError) as exc:
        await tools_mod.SubscriptionSkill().consultar({}, _ctx())
    assert exc.value.code == "sin_proveedor"

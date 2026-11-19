"""Enforcement de cuotas y estado operacional por billing (ADR-0045).

Genéricos: crean tenants desechables y, cuando hace falta, bajan el límite del
plan escribiendo directo en la suscripción (no se tocan datos de cliente real).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _onboard(slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant enforce {slug}",
        slug=slug,
        business_description="Enforcement de cuotas (test genérico).",
    )
    return tid


async def _set_subscription(tenant_id: uuid.UUID, **fields) -> None:
    """Actualiza la suscripción del tenant (RLS-scoped)."""
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tenant_id,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            for key, value in fields.items():
                setattr(sub, key, value)
            await session.commit()


async def test_tenant_con_trial_vigente_es_operacional():
    from core.billing.enforcement import is_tenant_operational

    tid = await _onboard(_uniq("enf-vigente"))
    assert await is_tenant_operational(tid) is True


async def test_trial_vencido_no_operacional():
    from core.billing.enforcement import is_tenant_operational

    tid = await _onboard(_uniq("enf-vencido"))
    past = datetime.now(UTC) - timedelta(days=1)
    await _set_subscription(tid, status="trialing", trial_ends_at=past, period_end=past)
    assert await is_tenant_operational(tid) is False


async def test_estado_bloqueante_no_operacional():
    from core.billing.enforcement import is_tenant_operational

    tid = await _onboard(_uniq("enf-status"))
    await _set_subscription(tid, status="past_due")
    assert await is_tenant_operational(tid) is False


async def test_sin_suscripcion_grandfathered():
    """Un tenant sin suscripción se considera operacional (no se corta por datos legados)."""
    from core.billing.enforcement import is_tenant_operational
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _onboard(_uniq("enf-sin"))
    async with tenant_scope(tid):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(TenantSubscription.tenant_id == tid)
                )
            ).scalar_one()
            await session.delete(sub)
            await session.commit()
    assert await is_tenant_operational(tid) is True


async def test_turn_quota_metric_por_costo_llm():
    """Bajar el límite de costo LLM a 0 agota la métrica de turno."""
    from core.billing import plans as plans_module
    from core.billing.enforcement import turn_quota_metric
    from core.billing.plans import Plan

    tid = await _onboard(_uniq("enf-turno"))
    original = plans_module.PLANS_BY_ID["free"]
    bumped = Plan(
        id="free",
        name="Free",
        price_cop=0,
        trial_days=30,
        limits={**original.limits, "costo_llm_usd": 0},
    )
    plans_module.PLANS_BY_ID["free"] = bumped
    try:
        assert await turn_quota_metric(tid) == "costo_llm_usd"
    finally:
        plans_module.PLANS_BY_ID["free"] = original


async def test_quota_decision_ilimitado_no_bloquea():
    from core.billing.enforcement import check_metric
    from core.billing.plans import PLANS_BY_ID

    tid = await _onboard(_uniq("enf-ilim"))
    original = PLANS_BY_ID["free"]
    from core.billing.plans import Plan

    unlimited = Plan(
        id="free", name="Free", price_cop=0, trial_days=30,
        limits={**original.limits, "conversaciones": None},
    )
    PLANS_BY_ID["free"] = unlimited
    try:
        assert await check_metric(tid, "conversaciones") is True
    finally:
        PLANS_BY_ID["free"] = original


async def _set_packs(tenant_id, packs: list[str]) -> None:
    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tenant_id,
                        AgentSpecRecord.is_active.is_(True),
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            spec = dict(row.spec)
            spec["packs"] = packs
            row.spec = spec
            await session.commit()


def _plan_con_tope_de_habilidades(monkeypatch) -> None:
    """`free` con el tope de habilidades de antes de ADR-0142 (`plugins: 1`), solo en el test."""
    from dataclasses import replace

    from core.billing import plans

    free = plans.PLANS_BY_ID["free"]
    monkeypatch.setitem(plans.PLANS_BY_ID, "free", replace(free, limits={**free.limits, "plugins": 1}))


async def test_cuota_plugins_cuenta_packs_de_negocio_no_el_core(monkeypatch):
    """Con un plan con `plugins: 1` (desde ADR-0142 ningún plan del catálogo lo define: el
    mecanismo se prueba con un plan de prueba): el primer pack de negocio entra; el segundo se bloquea.

    El gate se evalúa ANTES de agregar (`_enable_pack_for_tenant`), así que
    `used=0` con límite 1 permite el primero, y `used=1` bloquea el segundo.
    Antes del fix, `enabled_packs()` sumaba el core y un tenant recién creado ya
    veía `used=1` (2/1 si tenía un pack) → bloqueado sin poder habilitar nada.
    """
    from core.billing.enforcement import quota_decision

    _plan_con_tope_de_habilidades(monkeypatch)
    tid = await _onboard(_uniq("enf-pkg"))

    # Sin packs de negocio: hay cupo para habilitar el primero.
    d0 = await quota_decision(tid, "plugins")
    assert d0.allowed is True and d0.used == 0 and d0.limit == 1

    # Con 1 pack de negocio: la métrica NO suma el core (used==1, no 2), y ya no
    # hay cupo para un segundo pack (el gate se evalúa antes de agregar).
    await _set_packs(tid, ["pack_commerce"])
    d1 = await quota_decision(tid, "plugins")
    assert d1.used == 1, "el core no debe sumar a la métrica"
    assert d1.allowed is False

    # Con 2 packs: agotado igualmente.
    await _set_packs(tid, ["pack_commerce", "pack_calendar"])
    d2 = await quota_decision(tid, "plugins")
    assert d2.used == 2 and d2.allowed is False


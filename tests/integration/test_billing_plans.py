"""Planes, suscripciones y pagos: esquema, seed, alta y aislamiento (ADR-0045).

Genéricos: crean tenants desechables por test, nunca por cliente.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func, select

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
        name=f"Tenant billing {slug}",
        slug=slug,
        business_description="Planes y facturación (test genérico).",
    )
    return tid


async def test_plans_seeded():
    from core.billing.plans import PLANS
    from core.db.models import Plan
    from core.db.session import get_session

    async with get_session() as session:
        rows = (
            await session.execute(select(Plan).order_by(Plan.sort_order))
        ).scalars().all()

    assert [p.id for p in rows] == ["free", "basic", "premium", "empresarial"]
    assert {p.id for p in rows} == {p.id for p in PLANS}
    free = next(p for p in rows if p.id == "free")
    assert free.trial_days == 30
    assert free.price_cop == 0
    assert free.limits["clientes"] == 100
    empresarial = next(p for p in rows if p.id == "empresarial")
    assert empresarial.limits["clientes"] is None  # ilimitado


async def test_sync_plans_idempotente():
    from core.billing.plans import sync_plans
    from core.db.models import Plan
    from core.db.session import get_session

    assert await sync_plans() == 4
    assert await sync_plans() == 4
    async with get_session() as session:
        count = (await session.execute(select(func.count(Plan.id)))).scalar()
    assert count == 4


async def test_onboarding_crea_suscripcion_free_trial():
    from core.billing.service import get_active_subscription

    tid = await _onboard(_uniq("billing-alta"))
    sub = await get_active_subscription(tid)
    assert sub is not None
    assert sub.plan_id == "free"
    assert sub.status == "trialing"
    assert sub.trial_ends_at is not None
    assert sub.period_end == sub.trial_ends_at
    assert sub.trial_ends_at > sub.period_start


async def test_ensure_free_subscription_idempotente():
    from core.billing.service import ensure_free_subscription, get_active_subscription
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _onboard(_uniq("billing-idem"))
    async with tenant_scope(tid):
        async with get_session() as session:
            await ensure_free_subscription(session, tid)
            await session.commit()
    sub = await get_active_subscription(tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            count = (
                await session.execute(
                    select(func.count(TenantSubscription.id)).where(
                        TenantSubscription.tenant_id == tid,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar()
    assert count == 1
    assert sub.plan_id == "free"


async def test_suscripcion_aislada_entre_tenants():
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a = await _onboard(_uniq("billing-a"))
    b = await _onboard(_uniq("billing-b"))

    async with tenant_scope(a):
        async with get_session() as session:
            rows = (
                await session.execute(select(TenantSubscription.tenant_id))
            ).scalars().all()
    assert rows and all(t == a for t in rows), "RLS debe ocultar la suscripción de B"

    async with tenant_scope(b):
        async with get_session() as session:
            rows = (
                await session.execute(select(TenantSubscription.tenant_id))
            ).scalars().all()
    assert rows and all(t == b for t in rows)


async def test_metrica_plugins_no_cuenta_el_core():
    """El core es plomería del framework: no consume cuota (ADR-0045)."""
    from core.agents.spec import (
        count_tenant_packs,
        enabled_business_packs,
        enabled_packs,
        load_active_spec,
    )
    from core.billing.metrics import count_metric
    from core.shared.tenant import tenant_scope

    tid = await _onboard(_uniq("pkgnocore"))

    async with tenant_scope(tid):
        spec = await load_active_spec(tid)

    # `enabled_packs` (filtrado de tools) SÍ incluye el core...
    assert "core" in enabled_packs(spec)
    # ...pero la cuenta que consumen las cuotas no.
    assert "core" not in enabled_business_packs(spec)
    assert count_tenant_packs(spec) == len(set(spec.packs))
    assert count_tenant_packs(spec) == 0  # onboarding sin packs de negocio

    assert await count_metric(tid, "plugins") == 0


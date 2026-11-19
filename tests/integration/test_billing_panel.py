"""Página /admin/plan: barras de progreso, estado y planes (ADR-0045).

Genéricos: crean su propio tenant admin-visible por test.
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


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code in (200, 303), r.text
    return r.headers["set-cookie"].split(";")[0]


async def _switch_tenant(app_client, session_cookie: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant",
        headers={"Cookie": session_cookie},
        data={"tenant_id": str(tenant_id)},
        follow_redirects=False,
    )
    ac_tenant = next(c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session_cookie}; {ac_tenant}"


async def _new_tenant() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant plan {_uniq('x')}",
        slug=_uniq("plan-admin"),
        business_description="Panel de plan (test genérico).",
        admin_email="admin@local",
    )
    return tid


async def test_pagina_plan_muestra_trial_y_barras(app_client):
    tid = await _new_tenant()
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    r = await app_client.get("/admin/plan", headers={"Cookie": cookie})
    assert r.status_code == 200
    body = r.text
    assert "Free" in body
    assert "Prueba gratuita" in body
    assert "Clientes" in body
    assert "Conversaciones" in body
    assert "Basic" in body and "Premium" in body and "Empresarial" in body


async def test_pagina_plan_marca_limite_superado(app_client):
    """Rojo solo al SUPERAR el límite (used > limit), no al estar justo en el tope."""
    tid = await _new_tenant()
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    # Un cliente con límite 0: used (1) > limit (0) => superado.
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                Customer(tenant_id=tid, channel="whatsapp", external_id=f"wa:{_uniq('plan')}")
            )
            await session.commit()

    from core.billing import plans as plans_module
    from core.billing.plans import Plan

    original = plans_module.PLANS_BY_ID["free"]
    plans_module.PLANS_BY_ID["free"] = Plan(
        id="free", name="Free", price_cop=0, trial_days=30,
        limits={**original.limits, "clientes": 0},
    )
    try:
        r = await app_client.get("/admin/plan", headers={"Cookie": cookie})
        assert r.status_code == 200
        assert "Límite alcanzado" in r.text or "bg-danger" in r.text
    finally:
        plans_module.PLANS_BY_ID["free"] = original


async def test_pagina_plan_no_marca_danger_en_el_tope(app_client):
    """Estar justo en el tope (used == limit) NO es excedido: sin rojo."""
    tid = await _new_tenant()
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    from core.billing import plans as plans_module
    from core.billing.plans import Plan

    original = plans_module.PLANS_BY_ID["free"]
    # Límite 0 y 0 clientes del tenant nuevo: used (0) == limit (0).
    plans_module.PLANS_BY_ID["free"] = Plan(
        id="free", name="Free", price_cop=0, trial_days=30,
        limits={**original.limits, "clientes": 0},
    )
    try:
        r = await app_client.get("/admin/plan", headers={"Cookie": cookie})
        assert r.status_code == 200
        assert "Límite alcanzado" not in r.text
        assert "bg-danger" not in r.text
    finally:
        plans_module.PLANS_BY_ID["free"] = original


async def test_dashboard_muestra_resumen_de_plan(app_client):
    tid = await _new_tenant()
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    r = await app_client.get("/admin", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Plan" in r.text


async def test_plan_vencido_muestra_banner_de_servicio_detenido(app_client):
    tid = await _new_tenant()
    cookie = await _switch_tenant(app_client, await _login(app_client), tid)

    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    past = datetime.now(UTC) - timedelta(days=1)
    async with tenant_scope(tid):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(TenantSubscription.tenant_id == tid)
                )
            ).scalar_one()
            sub.status = "trialing"
            sub.trial_ends_at = past
            sub.period_end = past
            await session.commit()

    r = await app_client.get("/admin", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Servicio detenido" in r.text

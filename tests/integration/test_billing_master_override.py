"""Cambio de plan por un admin master desde /admin/tenants (ADR-0045).

Genérico: crea un tenant desechable visible por el admin de tests.
"""

from __future__ import annotations

import os
import uuid

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


async def _new_tenant() -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant master {_uniq('x')}",
        slug=_uniq("master-plan"),
        admin_email="admin@local",
    )
    return tid


async def test_tenants_lista_muestra_plan(app_client):
    await _new_tenant()
    cookie = await _login(app_client)
    r = await app_client.get("/admin/tenants", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Plan" in r.text
    assert "Free" in r.text


async def test_master_cambia_plan_y_extiende(app_client):
    from core.billing.service import get_active_subscription
    from core.db.models import AuditLog
    from core.db.session import get_session

    tid = await _new_tenant()
    cookie = await _login(app_client)

    r = await app_client.post(
        f"/admin/tenants/{tid}/plan",
        headers={"Cookie": cookie},
        data={"plan_id": "premium", "extend_days": "10"},
    )
    assert r.status_code in (200, 303)

    sub = await get_active_subscription(tid)
    assert sub.plan_id == "premium"
    assert sub.status == "active"
    assert sub.period_end is not None

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            logged = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tid,
                        AuditLog.action == "billing.set_plan",
                    )
                )
            ).scalar_one_or_none()
    assert logged is not None
    assert logged.input["plan_id"] == "premium"

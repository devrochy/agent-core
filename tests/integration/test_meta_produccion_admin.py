"""Checklist de puesta en marcha en la API nueva
(`/api/v1/admin/meta/checklist/*`): requisitos manuales separados de los checks
automáticos y persistencia de pasos manuales. La corrida de producción vive en
`test_checklist_produccion.py` (HU-CONN-07)."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _new_tenant() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("produccion-api")
    tid, _ = await onboard_tenant(
        name=f"Tenant producción {slug}",
        slug=slug,
        business_description="Test de checklist de producción.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    return tid


def _fake_preflight_checks(ok: bool):
    from datetime import UTC, datetime

    from core.gateway.meta_preflight import PreflightCheck

    def _make(key: str) -> PreflightCheck:
        return PreflightCheck(
            key=key,
            ok=ok,
            detail="todo bien" if ok else "forzado a fallar",
            checked_at=datetime.now(UTC),
        )

    return [
        _make(k)
        for k in (
            "webhook_handshake",
            "app_subscriptions_complete",
            "prod_secret_differs_from_dev",
            "token_keys_configured",
            "worker_heartbeat_and_dlq",
            "rls_forced",
            "rls_tests_pass",
            "no_secret_leak_in_templates",
            "connections_health_except_billing",
        )
    ]


async def test_requisitos_separan_pasos_manuales_y_checks_automaticos(app_client, monkeypatch):
    import core.gateway.meta_preflight as preflight_mod

    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    async def _fake_run_preflight(tenant_id, slug=""):
        return _fake_preflight_checks(ok=False)

    monkeypatch.setattr(preflight_mod, "run_preflight", _fake_run_preflight)

    r = await app_client.get("/api/v1/admin/meta/checklist/requirements", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # Pasos manuales con su link a la app.
    assert body["steps"], "deben venir los pasos manuales"
    assert all(paso["key"] and paso["label"] and paso["link"] for paso in body["steps"])
    # Checks automáticos: los 9, con su estado.
    assert len(body["checks"]) == 9
    assert all(check["ok"] is False for check in body["checks"])
    assert "meta_configured" in body


async def test_marcar_paso_manual_persiste_notas(app_client):
    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    r = await app_client.post(
        "/api/v1/admin/meta/checklist/step",
        headers=headers,
        json={"step_key": "advanced_access", "done": True, "notes": "concedido el 2026-09-20"},
    )
    assert r.status_code == 204, r.text

    from sqlalchemy import select

    from core.db.models import MetaSetupChecklist
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(MetaSetupChecklist).where(
                        MetaSetupChecklist.tenant_id == tid,
                        MetaSetupChecklist.step_key == "advanced_access",
                    )
                )
            ).scalar_one()
    assert row.done is True
    assert row.notes == "concedido el 2026-09-20"

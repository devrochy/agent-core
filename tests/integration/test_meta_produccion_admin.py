"""Checklist de puesta en marcha en la API nueva
(`/api/v1/admin/meta/checklist/*`): requisitos manuales separados de los checks
automáticos, corrida on-demand de producción y persistencia de pasos manuales."""

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
    from core.onboarding import onboard_tenant

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


async def test_run_produccion_devuelve_el_estado_de_los_9_checks(app_client, monkeypatch):
    import core.gateway.meta_production_readiness as readiness_mod

    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    async def _fake_check_fail(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=False, detail="forzado a fallar", checked_at=datetime.now(UTC))

    async def _fake_check_ok(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=True, detail="todo bien", checked_at=datetime.now(UTC))

    def _fake_sync_fail(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=False, detail="forzado a fallar", checked_at=datetime.now(UTC))

    def _fake_sync_ok(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=True, detail="todo bien", checked_at=datetime.now(UTC))

    # Todos fallan: el panel deshabilita "listo para producción" si algún automático
    # está en rojo (la lógica de botón es del panel; acá se verifica el dato).
    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _fake_check_fail)
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _fake_check_fail)
    monkeypatch.setattr(readiness_mod, "check_prod_secret_differs_from_dev", _fake_sync_fail)
    monkeypatch.setattr(readiness_mod, "check_token_keys_configured", _fake_sync_fail)
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _fake_check_fail)
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _fake_check_fail)
    monkeypatch.setattr(readiness_mod, "check_rls_tests_pass", _fake_sync_fail)
    monkeypatch.setattr(readiness_mod, "check_no_secret_leak_in_templates", _fake_sync_fail)
    monkeypatch.setattr(readiness_mod, "check_connections_health_except_billing", _fake_check_fail)

    r = await app_client.post("/api/v1/admin/meta/checklist/production/run", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 9
    assert all(check["ok"] is False for check in r.json())

    # Todos pasan: el dato para habilitar el botón.
    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _fake_check_ok)
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _fake_check_ok)
    monkeypatch.setattr(readiness_mod, "check_prod_secret_differs_from_dev", _fake_sync_ok)
    monkeypatch.setattr(readiness_mod, "check_token_keys_configured", _fake_sync_ok)
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _fake_check_ok)
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _fake_check_ok)
    monkeypatch.setattr(readiness_mod, "check_rls_tests_pass", _fake_sync_ok)
    monkeypatch.setattr(readiness_mod, "check_no_secret_leak_in_templates", _fake_sync_ok)
    monkeypatch.setattr(readiness_mod, "check_connections_health_except_billing", _fake_check_ok)

    r2 = await app_client.post("/api/v1/admin/meta/checklist/production/run", headers=headers)
    assert r2.status_code == 200
    assert all(check["ok"] is True for check in r2.json())


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

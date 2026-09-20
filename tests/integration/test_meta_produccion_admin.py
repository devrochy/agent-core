"""Criterio de aceptación explícito: la página muestra automáticos y
manuales por separado, y no permite marcar "listo para producción" si
algún automático está en rojo."""

from __future__ import annotations

import os
import uuid

import pytest

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
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _switch_tenant(app_client, session_cookie: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session_cookie}, data={"tenant_id": str(tenant_id)}
    )
    ac_tenant = next(c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session_cookie}; {ac_tenant}"


async def _new_tenant():
    from core.onboarding import onboard_tenant

    slug = _uniq("produccion-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant producción {slug}", slug=slug, business_description="Test de checklist de producción.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


async def test_pagina_muestra_automaticos_y_manuales_separados_sin_correr(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get("/admin/conexiones/produccion", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Checks automáticos" in r.text
    assert "Pasos manuales" in r.text
    # Sin correr todavía -- no debe haber ninguna fila de resultado automático.
    assert "Todavía no se corrieron" in r.text


async def test_boton_listo_para_produccion_deshabilitado_si_algun_automatico_falla(app_client, monkeypatch):
    import core.gateway.meta_production_readiness as readiness_mod

    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    async def _fake_check(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=False, detail="forzado a fallar", checked_at=datetime.now(UTC))

    def _fake_check_sync(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=False, detail="forzado a fallar", checked_at=datetime.now(UTC))

    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _fake_check)
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _fake_check)
    monkeypatch.setattr(readiness_mod, "check_prod_secret_differs_from_dev", _fake_check_sync)
    monkeypatch.setattr(readiness_mod, "check_token_keys_configured", _fake_check_sync)
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _fake_check)
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _fake_check)
    monkeypatch.setattr(readiness_mod, "check_rls_tests_pass", _fake_check_sync)
    monkeypatch.setattr(readiness_mod, "check_no_secret_leak_in_templates", _fake_check_sync)
    monkeypatch.setattr(readiness_mod, "check_connections_health_except_billing", _fake_check)

    r = await app_client.post("/admin/conexiones/produccion/ejecutar", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "disabled" in r.text
    assert "Todavía no" in r.text


async def test_boton_listo_para_produccion_habilitado_si_todos_los_automaticos_pasan(app_client, monkeypatch):
    import core.gateway.meta_production_readiness as readiness_mod

    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    async def _fake_ok(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=True, detail="todo bien", checked_at=datetime.now(UTC))

    def _fake_ok_sync(*a, **k):
        from datetime import UTC, datetime

        from core.gateway.meta_preflight import PreflightCheck

        return PreflightCheck(key="fake", ok=True, detail="todo bien", checked_at=datetime.now(UTC))

    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _fake_ok)
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _fake_ok)
    monkeypatch.setattr(readiness_mod, "check_prod_secret_differs_from_dev", _fake_ok_sync)
    monkeypatch.setattr(readiness_mod, "check_token_keys_configured", _fake_ok_sync)
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _fake_ok)
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _fake_ok)
    monkeypatch.setattr(readiness_mod, "check_rls_tests_pass", _fake_ok_sync)
    monkeypatch.setattr(readiness_mod, "check_no_secret_leak_in_templates", _fake_ok_sync)
    monkeypatch.setattr(readiness_mod, "check_connections_health_except_billing", _fake_ok)

    r = await app_client.post("/admin/conexiones/produccion/ejecutar", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "disabled" not in r.text
    assert "Todavía no" not in r.text


async def test_marcar_paso_manual_persiste_notas(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.post(
        "/admin/conexiones/meta-requisitos/paso", headers={"Cookie": cookie},
        data={"step_key": "advanced_access", "done": "on", "notes": "concedido el 2026-09-20"},
    )
    assert r.status_code == 303

    from sqlalchemy import select

    from core.db.models import MetaSetupChecklist
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(MetaSetupChecklist).where(
                        MetaSetupChecklist.tenant_id == tid, MetaSetupChecklist.step_key == "advanced_access"
                    )
                )
            ).scalar_one()
    assert row.done is True
    assert row.notes == "concedido el 2026-09-20"

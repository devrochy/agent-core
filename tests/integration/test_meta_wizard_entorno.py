"""Criterio de aceptación: la pantalla de elegir entorno arranca el wizard
con el `environment` correcto, el stepper refleja `completed_steps`/
`current_step`, el aviso de requisitos incompletos solo aparece en modo
producción, y "Probar conexión" corre los checks reales y persiste el
resultado en `MetaConnection.health` (mismo criterio que "Diagnosticar")."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

from core.config import get_settings

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


async def _new_tenant() -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = _uniq("wizard-entorno")
    tid, _ = await onboard_tenant(
        name=f"Tenant wizard entorno {slug}", slug=slug, business_description="Test de entorno del wizard.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


def _configure_meta_app_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")
    monkeypatch.setattr(settings, "meta_config_id", "cfg-embedded-signup")


async def test_pantalla_elegir_entorno_muestra_las_dos_opciones(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get("/admin/conexiones/wizard/whatsapp/elegir-entorno", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Modo prueba" in r.text
    assert "Modo producción" in r.text
    # Modo prueba ahora abre el modal BYOK (no navega directo a Embedded Signup).
    assert 'data-bs-target="#modal-byok-test"' in r.text
    assert 'href="/admin/conexiones/wizard/whatsapp?environment=production"' in r.text


async def test_elegir_modo_prueba_arranca_wizard_sandbox(app_client, monkeypatch):
    from core.db.models import ConnectionWizard
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get(
        "/admin/conexiones/wizard/whatsapp?environment=sandbox", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    assert "Modo prueba" in r.text

    async with tenant_scope(tid):
        async with get_session() as session:
            wizard = (
                await session.execute(select(ConnectionWizard).where(ConnectionWizard.tenant_id == tid))
            ).scalar_one()
    assert wizard.environment == "sandbox"


async def test_elegir_modo_produccion_arranca_wizard_production(app_client, monkeypatch):
    from core.db.models import ConnectionWizard
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get(
        "/admin/conexiones/wizard/whatsapp?environment=production", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    assert "Modo producción" in r.text

    async with tenant_scope(tid):
        async with get_session() as session:
            wizard = (
                await session.execute(select(ConnectionWizard).where(ConnectionWizard.tenant_id == tid))
            ).scalar_one()
    assert wizard.environment == "production"


async def test_produccion_con_checklist_incompleto_muestra_aviso(app_client, monkeypatch):
    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get(
        "/admin/conexiones/wizard/whatsapp?environment=production", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    assert "Todavía faltan requisitos de Meta para producción" in r.text


async def test_produccion_con_checklist_completo_no_muestra_aviso(app_client, monkeypatch):
    from core.gateway.meta_preflight import MANUAL_STEPS

    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    for step in MANUAL_STEPS:
        r = await app_client.post(
            "/admin/conexiones/meta-requisitos/paso", headers={"Cookie": cookie},
            data={"step_key": step["key"], "done": "on"},
        )
        assert r.status_code == 303

    r = await app_client.get(
        "/admin/conexiones/wizard/whatsapp?environment=production", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    assert "Todavía faltan requisitos de Meta para producción" not in r.text


async def test_modo_test_no_muestra_aviso_de_requisitos(app_client, monkeypatch):
    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get(
        "/admin/conexiones/wizard/whatsapp?environment=sandbox", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    assert "Todavía faltan requisitos de Meta para producción" not in r.text


async def test_stepper_marca_pasos_completados_y_el_actual(app_client, monkeypatch):
    _configure_meta_app_settings(monkeypatch)
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get(
        "/admin/conexiones/wizard/whatsapp?environment=sandbox", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    # Arranca en select_channel: aparece como paso listado (pendiente o actual).
    assert "select_channel" in r.text
    assert "register_number" in r.text
    assert "done" in r.text


async def test_probar_conexion_ok_persiste_health_y_redirige_con_flash_exito(app_client, monkeypatch):
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_production_readiness as readiness_mod
    from core.gateway.meta_preflight import PreflightCheck
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    waba_id, phone_id = _uniq("waba"), _uniq("phone")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", meta_app_id="app-x",
                status="connected", business_id="biz-probar",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id, name="Mi WABA")
            session.add(waba)
            await session.flush()
            session.add(PhoneNumber(tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id, phone_number_id=phone_id))
            connection_id = connection.id

    async def _fake_handshake(*a, **k):
        from datetime import UTC, datetime

        return PreflightCheck(key="webhook_handshake", ok=True, detail="ok", checked_at=datetime.now(UTC))

    async def _fake_run_health_checks(connection, tenant_id):
        return {"token_valid": {"ok": True}, "webhook_subscribed": {"ok": True}}

    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _fake_handshake)
    import core.gateway.meta_health as health_mod

    monkeypatch.setattr(health_mod, "run_health_checks", _fake_run_health_checks)
    monkeypatch.setattr(health_mod, "decide_status", lambda checks: ("connected", None))

    r = await app_client.post(
        "/admin/conexiones/wizard/whatsapp/probar-conexion",
        headers={"Cookie": cookie}, data={"connection_id": str(connection_id)},
    )
    assert r.status_code == 303
    assert "flash_type=success" in r.headers["location"]

    async with tenant_scope(tid):
        async with get_session() as session:
            row = await session.get(MetaConnection, connection_id)
    assert row.health == {"token_valid": {"ok": True}, "webhook_subscribed": {"ok": True}}
    assert row.status == "connected"


async def test_probar_conexion_de_otro_tenant_no_encuentra_ni_modifica_nada(app_client):
    """Hallazgo del auditor-multitenant: falta un test explícito de que
    pasar a mano el `connection_id` de OTRO tenant no encuentra la fila
    (get_owned filtra por tenant_id) ni la modifica."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_a = await _new_tenant()
    async with tenant_scope(tenant_a):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tenant_a, channel="whatsapp", environment="sandbox", meta_app_id="app-x",
                status="connected", business_id="biz-tenant-a",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
            original_health = dict(connection.health)
            original_status = connection.status

    tenant_b = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tenant_b)

    r = await app_client.post(
        "/admin/conexiones/wizard/whatsapp/probar-conexion",
        headers={"Cookie": cookie}, data={"connection_id": str(connection_id)},
    )
    assert r.status_code == 303
    assert "flash_type=danger" in r.headers["location"]

    async with tenant_scope(tenant_a):
        async with get_session() as session:
            row = await session.get(MetaConnection, connection_id)
    assert row.health == original_health
    assert row.status == original_status

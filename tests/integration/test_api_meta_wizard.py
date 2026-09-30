"""Wizard de WhatsApp en la API nueva (`/api/v1/admin/meta/wizard*`, ADR-0080).

Porta las garantías del wizard clásico: BYOK con credenciales rechazadas no crea
nada, con credenciales válidas crea conexión + número + WABA (suscripta), y si
Meta no confirma la suscripción la conexión queda en `error` (bug real en vivo).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _new_tenant() -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = uniq("wizard-api")
    tid, _ = await onboard_tenant(
        name=f"Tenant wizard api {slug}",
        slug=slug,
        business_description="Test del wizard de la API.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    return tid


def _payload(app_id: str) -> dict[str, str]:
    return {
        "app_id": app_id,
        "app_secret": "secret-byok",
        "phone_number_id": uniq("phone-byok"),
        "access_token": "tok-byok",
        "waba_id": uniq("waba-byok"),
    }


async def _fake_verify_ok(phone_number_id, access_token, app_secret):
    return True, "ok", {"display_phone_number": "+1 555 0100", "verified_name": "Mi Negocio Test"}


async def _fake_graph_ok(path, *, method="GET", **kwargs):
    if method == "POST":
        return {"success": True}
    return {"data": [{"whatsapp_business_api_data": {"id": "app-x"}}]}


async def _fake_graph_sin_confirmar(path, *, method="GET", **kwargs):
    if method == "POST":
        return {"success": True}
    return {"data": []}  # Meta acepta el POST pero no confirma la suscripción.


def _fake_upsert_factory(llamadas: list):
    async def _fake_upsert_app_subscription(config, object_, *, callback_url, verify_token):
        llamadas.append((config.app_id, object_, callback_url))

        class _Status:
            pass

        return _Status()

    return _fake_upsert_app_subscription


async def test_byok_requiere_sesion(app_client) -> None:
    r = await app_client.post("/api/v1/admin/meta/wizard/byok", json=_payload("app-x"))
    assert r.status_code == 401


async def test_wizard_state_sin_app_configurada(app_client, monkeypatch) -> None:
    from core.config import get_settings

    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "")
    monkeypatch.setattr(settings, "meta_config_id", "")

    r = await app_client.get("/api/v1/admin/meta/wizard", headers=headers)
    assert r.status_code == 200
    assert r.json()["meta_configured"] is False
    assert r.json()["environments"] == ["sandbox", "production"]


async def test_byok_con_credenciales_invalidas_no_crea_nada(app_client, monkeypatch) -> None:
    import core.gateway.meta_byok as byok_mod
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    async def _fake_verify_fail(phone_number_id, access_token, app_secret):
        return False, "Meta rechazó las credenciales: token inválido", {}

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _fake_verify_fail)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", headers=headers, json=_payload("app-byok-invalida")
    )
    assert r.status_code == 400
    assert "credenciales" in r.text

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tid,
                        MetaConnection.meta_app_id == "app-byok-invalida",
                    )
                )
            ).scalar_one_or_none()
    assert connection is None


async def test_byok_valido_crea_conexion_numero_y_waba(app_client, monkeypatch) -> None:
    import core.gateway.meta_byok as byok_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _fake_verify_ok)
    monkeypatch.setattr(byok_mod, "graph_fetch", _fake_graph_ok)
    llamadas: list = []
    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _fake_upsert_factory(llamadas))

    payload = _payload(uniq("app-byok"))
    r = await app_client.post("/api/v1/admin/meta/wizard/byok", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["connectionId"]

    assert len(llamadas) == 1
    assert llamadas[0][0] == payload["app_id"]
    assert llamadas[0][1] == "whatsapp_business_account"

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tid,
                        MetaConnection.meta_app_id == payload["app_id"],
                    )
                )
            ).scalar_one()
            phone = (
                await session.execute(
                    select(PhoneNumber).where(PhoneNumber.connection_id == connection.id)
                )
            ).scalar_one()
            waba = (
                await session.execute(
                    select(WabaAccount).where(WabaAccount.connection_id == connection.id)
                )
            ).scalar_one()

    assert connection.status == "connected"
    assert connection.environment == "sandbox"
    assert phone.phone_number_id == payload["phone_number_id"]
    assert phone.registered is True
    assert waba.waba_id == payload["waba_id"]
    assert waba.subscribed is True


async def test_byok_sin_suscripcion_confirmada_queda_en_error(app_client, monkeypatch) -> None:
    """Bug real en vivo: si Meta no confirma `subscribed_apps`, no llegan mensajes:
    la conexión debe quedar en `error` (nunca en `connected`) y la API avisarlo."""
    import core.gateway.meta_byok as byok_mod
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _fake_verify_ok)
    monkeypatch.setattr(byok_mod, "graph_fetch", _fake_graph_sin_confirmar)

    payload = _payload(uniq("app-byok-sinsub"))
    r = await app_client.post("/api/v1/admin/meta/wizard/byok", headers=headers, json=payload)
    assert r.status_code == 400, r.text
    assert "suscripción" in r.text

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tid,
                        MetaConnection.meta_app_id == payload["app_id"],
                    )
                )
            ).scalar_one()
    assert connection.status == "error"
    assert "suscripción" in (connection.status_reason or "")


async def test_manual_con_token_invalido_no_crea_nada(app_client, monkeypatch) -> None:
    import core.gateway.meta_health as health_mod
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    headers = await api_headers(app_client, tid)

    class _Check:
        ok = False
        detail = "Token vencido o inválido."

    async def _fake_check_token(*a, **k):
        return _Check()

    monkeypatch.setattr(health_mod, "check_token_valid", _fake_check_token)

    r = await app_client.post(
        "/api/v1/admin/meta/manual",
        headers=headers,
        json={"waba_id": "waba-x", "phone_number_id": "phone-x", "token": "tok-x"},
    )
    assert r.status_code == 400

    async with tenant_scope(tid):
        async with get_session() as session:
            cuantas = (
                await session.execute(
                    select(MetaConnection).where(MetaConnection.tenant_id == tid)
                )
            ).scalars().all()
    assert cuantas == []

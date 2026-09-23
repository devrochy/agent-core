"""Configuración por tabs y pestaña de Pagos del pack-payments (ADR-0049).

Cubre: la tab "Pagos" solo aparece con el pack habilitado; la pantalla de pagos
renderiza el formulario y la URL del webhook; y el "probar y guardar" valida la
llave contra Wompi (mockeado) sin persistir si falla.
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

PASSWORD = "clave-segura-123"


async def _tenant_id() -> uuid.UUID:
    """Primer tenant del admin de test (el que el middleware resuelve sin cookie)."""
    from sqlalchemy import select

    from core.db.models import Admin
    from core.db.session import get_session
    from core.shared.tenant import admin_tenants

    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == "admin@local"))
        ).scalar_one()
    tenants = await admin_tenants(admin.id)
    assert tenants, "admin@local no tiene tenants (correr `agent-core db seed`)"
    return tenants[0][0]


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _switch(app_client, session: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tenant_id)}
    )
    cookies = [c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c]
    assert cookies, "switch-tenant no fijó ac_tenant"
    return f"{session}; {cookies[0]}"


async def _set_pack(tenant_id: uuid.UUID, pack: str, enabled: bool) -> None:
    from core.agents.spec import load_active_spec
    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        await load_active_spec()
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AgentSpecRecord)
                    .where(
                        AgentSpecRecord.tenant_id == tenant_id,
                        AgentSpecRecord.is_active.is_(True),
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                    .order_by(AgentSpecRecord.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            data = dict(row.spec)
            packs = set(data.get("packs", []))
            packs.add(pack) if enabled else packs.discard(pack)
            data["packs"] = sorted(packs)
            row.spec = data


async def test_tab_pagos_solo_con_pack_habilitado(app_client) -> None:
    tid = await _tenant_id()
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    await _set_pack(tid, "pack_payments", False)
    r = await app_client.get("/admin/configuracion", headers=headers)
    assert r.status_code == 200
    assert "/admin/plugins/pack_payments/config" not in r.text
    assert "Secciones de configuración" in r.text

    await _set_pack(tid, "pack_payments", True)
    r = await app_client.get("/admin/configuracion", headers=headers)
    assert r.status_code == 200
    assert "/admin/plugins/pack_payments/config" in r.text


async def test_pantalla_pagos_renderiza_webhook(app_client) -> None:
    tid = await _tenant_id()
    await _set_pack(tid, "pack_payments", True)
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    r = await app_client.get("/admin/plugins/pack_payments/config", headers=headers)
    assert r.status_code == 200
    assert "Pasarela de pagos" in r.text
    assert "/webhooks/payments/wompi/" in r.text


async def test_probar_y_guardar_no_persiste_si_wompi_falla(app_client, monkeypatch) -> None:
    import pack_payments.providers.wompi as wompi_mod
    from pack_payments.providers.base import PaymentRequestRejected

    from core.plugin_manager import plugin_manager
    from core.shared.exceptions import PluginConfigError
    from core.shared.tenant import tenant_scope

    async def _boom(self, **kwargs):
        raise PaymentRequestRejected("llave inválida")

    monkeypatch.setattr(wompi_mod.WompiProvider, "create_payment_link", _boom)
    tid = await _tenant_id()
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    r = await app_client.post(
        "/admin/plugins/pack_payments/config",
        headers=headers,
        data={
            "public_key": "pub_test_x",
            "private_key": "prv_test_x",
            "events_secret": "test_events_x",
            "test": "1",
        },
    )
    assert r.status_code == 303
    assert "danger" in r.headers["location"]

    async with tenant_scope(tid):
        # `load_config` lanza PluginConfigError si el tenant no tiene config
        # (plugin `requires_config`): eso ES "no se persistió".
        with pytest.raises(PluginConfigError):
            await plugin_manager.load_config("pack_payments")


async def test_url_directa_sin_pack_habilitado_rechaza(app_client) -> None:
    tid = await _tenant_id()
    await _set_pack(tid, "pack_payments", False)
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    r = await app_client.get("/admin/plugins/pack_payments/config", headers=headers)
    assert r.status_code == 303
    assert "configuracion" in r.headers["location"]
    assert "habilitado" in r.headers["location"]


async def test_sin_sesion_redirige_a_login(app_client) -> None:
    # `app_client` es module-scoped y conserva cookies de otros tests: se manda
    # una sesión vacía explícita para simular "sin login".
    r = await app_client.get(
        "/admin/plugins/pack_payments/config",
        headers={"Cookie": ""},
        follow_redirects=False,
    )
    assert r.status_code in (303, 307)
    assert r.headers["location"] == "/admin/login"


async def test_colaborador_sin_settings_write_rechaza(app_client, tenant_setup) -> None:
    """Un colaborador (read en settings) no puede guardar credenciales de pago."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    t1, _t2 = tenant_setup
    email = f"colab-pagos-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=t1, admin_id=admin.id, role="colaborador"))

    r = await app_client.post("/admin/login", data={"email": email, "password": PASSWORD})
    cookie = r.headers["set-cookie"].split(";")[0]
    r = await app_client.post(
        "/admin/plugins/pack_payments/config",
        headers={"Cookie": cookie},
        data={"public_key": "pub_test_x", "private_key": "prv_test_x", "events_secret": "s"},
    )
    assert r.status_code in (303, 403)
    if r.status_code == 303:
        assert "sin_permiso" in r.headers["location"]

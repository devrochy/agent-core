"""Renderizador de vistas por descriptor (Fase D, Sprint 4): el pack registra
tabla+form+handlers y el core sirve el HTML genérico y el guardado.

Usa el tenant por defecto (el que usa el middleware sin cookie): consistente
entre la lectura, el POST y la verificación, sin depender del seed del framework.
"""

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


@pytest.fixture(scope="module", autouse=True)
async def _pack_views_activo():
    """Activa pack_commerce (registra skills + vistas por descriptor) igual que el
    arranque real (activate_all_configured). ASGITransport no corre el lifespan,
    así que ni el registro en la tabla `plugins` (sync_registry) ni la activación
    ocurren solos -- FK de `plugin_configs` a `plugins` exige que exista la fila."""
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import default_tenant, tenant_scope

    plugin_manager.scan()
    await plugin_manager.sync_registry()
    tenant_id = await default_tenant()
    async with tenant_scope(tenant_id):
        # pack_commerce.requires_config: necesita una fila de config (aunque
        # sea vacía) antes de poder activarse para este tenant.
        await plugin_manager.save_config("pack_commerce", {})
        await plugin_manager.activate("pack_commerce")
    yield


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _login_as_default(app_client) -> str:
    """Login + cookie ac_tenant del tenant por defecto (el del middleware sin cookie).

    El middleware usa el PRIMER tenant del admin (orden arbitrario de admin_tenants);
    fijar la cookie hace el test determinístico y consistente con default_tenant().
    """
    from core.shared.tenant import default_tenant

    session = await _login(app_client)
    r = await app_client.post(
        "/admin/switch-tenant",
        headers={"Cookie": session},
        data={"tenant_id": str(await default_tenant())},
    )
    ac_tenant = next(
        c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c
    )
    return f"{session}; {ac_tenant}"


async def _ensure_test_customer() -> str:
    """Crea (si falta) un Customer de prueba en el tenant por defecto. Devuelve su id.

    "Leads" en el panel ES la lista de `Customer` del tenant (todo cliente que
    escribió es un lead, `_leads_list` en pack_commerce/module.py) -- `Lead` es
    solo el enriquecimiento opcional de nombre/interés, keyed por customer_id,
    no la entidad que lista/edita el panel."""
    from sqlalchemy import select

    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import default_tenant, tenant_scope

    tenant_id = await default_tenant()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            customer = (
                await session.execute(
                    select(Customer).where(
                        Customer.tenant_id == tenant_id,
                        Customer.external_id == "test-view-descriptor",
                    )
                )
            ).scalar_one_or_none()
            if customer is None:
                customer = Customer(
                    tenant_id=tenant_id, channel="whatsapp", external_id="test-view-descriptor"
                )
                session.add(customer)
                await session.flush()
            return str(customer.id)


async def test_vista_descriptor_renderiza(app_client) -> None:
    cookie = await _login_as_default(app_client)
    r = await app_client.get(
        "/admin/packs/pack_commerce/leads", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    assert "Leads" in r.text
    assert "modal-leads" in r.text, "debe haber modal de edición genérico"
    assert "dt-search" in r.text, "debe usar el motor data-table"
    # El form de edición solo tiene name/interest (enriquecimiento manual) --
    # estado/segmento se calculan solos, no son campos editables (module.py:1071-1078).
    assert 'name="name"' in r.text and 'name="interest"' in r.text


async def test_vista_descriptor_guarda(app_client) -> None:
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import default_tenant, tenant_scope

    customer_id = await _ensure_test_customer()
    cookie = await _login_as_default(app_client)
    r = await app_client.post(
        "/admin/packs/pack_commerce/leads/guardar",
        headers={"Cookie": cookie},
        data={"record_id": customer_id, "name": "Lead de prueba", "interest": "vista descriptor"},
    )
    assert r.status_code == 303

    async with tenant_scope(await default_tenant()):
        async with get_session() as session:
            lead = (
                await session.execute(
                    select(Lead).where(Lead.customer_id == uuid.UUID(customer_id))
                )
            ).scalar_one_or_none()
    assert lead is not None, "guardar debe crear el Lead de enriquecimiento si no existía"
    assert lead.name == "Lead de prueba" and lead.interest == "vista descriptor"
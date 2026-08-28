"""Renderizador de vistas por descriptor (Fase D, Sprint 4): el pack registra
tabla+form+handlers y el core sirve el HTML genérico y el guardado."""

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
    arranque real (activate_all_configured). ASGITransport no corre el lifespan."""
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import default_tenant, tenant_scope

    plugin_manager.scan()
    tenant_id = await default_tenant()
    async with tenant_scope(tenant_id):
        await plugin_manager.activate("pack_commerce")
    yield


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _first_lead_id() -> str | None:
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope("00000000-0000-0000-0000-000000000001"):
        async with get_session() as session:
            lead = (
                await session.execute(select(Lead.id).order_by(Lead.created_at.desc()).limit(1))
            ).scalar_one_or_none()
    return str(lead) if lead else None


async def test_vista_descriptor_renderiza(app_client) -> None:
    cookie = await _login(app_client)
    r = await app_client.get(
        "/admin/packs/pack_commerce/leads", headers={"Cookie": cookie}
    )
    assert r.status_code == 200
    assert "Leads" in r.text
    assert "modal-leads" in r.text, "debe haber modal de edición genérico"
    assert "dt-search" in r.text, "debe usar el motor data-table"
    assert 'name="score"' in r.text and 'name="status"' in r.text


async def test_vista_descriptor_guarda(app_client) -> None:
    lead_id = await _first_lead_id()
    if lead_id is None:
        pytest.skip("sin leads para probar el guardado")
    cookie = await _login(app_client)
    headers = {"Cookie": cookie}
    r = await app_client.post(
        "/admin/packs/pack_commerce/leads/guardar",
        headers=headers,
        data={"record_id": lead_id, "score": "9", "status": "calificado"},
    )
    assert r.status_code == 303


    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope("00000000-0000-0000-0000-000000000001"):
        async with get_session() as session:
            lead = await session.get(Lead, uuid.UUID(lead_id))
    assert lead is not None
    assert lead.score == 9 and lead.status == "calificado"
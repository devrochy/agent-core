"""Flujo completo del panel de plantillas: crear (sin cabecera multimedia),
sincronizar (idempotente contra la BD real), webhook de estado actualiza el
listado, borrado de una traducción vs. todas."""

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


async def _setup_connected_tenant():
    from core.db.models import MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = _uniq("plantillas-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant plantillas {slug}", slug=slug, business_description="Test de plantillas.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    waba_id = _uniq("waba")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", meta_app_id="app-embedded-signup",
                status="connected", business_id="biz-plantillas",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
            session.add(waba)
            await session.flush()
            connection_id, waba_row_id = connection.id, waba.id

        await meta_token_store.put_token(connection_id, TOKEN_KIND, "biz-plantillas", "tok-plantillas", tenant_id=tid)
    return tid, connection_id, waba_id, waba_row_id


def _configure_meta_app_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")


async def test_crear_sincronizar_webhook_y_borrar(app_client, monkeypatch):
    import core.gateway.meta_template_sync as sync_mod
    import core.gateway.meta_templates as templates_mod

    _configure_meta_app_settings(monkeypatch)
    tid, connection_id, waba_id, _waba_row_id = await _setup_connected_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    meta_template_id = _uniq("meta-tpl")

    async def _fake_graph_fetch(path, *, method="GET", body=None, query=None, **kwargs):
        if path == f"/{waba_id}/message_templates" and method == "POST":
            return {"id": meta_template_id, "status": "PENDING"}
        if path == f"/{waba_id}/message_templates" and method == "DELETE":
            return {"success": True}
        if path == f"/{waba_id}/message_templates" and query and query.get("summary"):
            return {"summary": {"message_template_count": 1, "message_template_limit": 250, "are_translations_complete": True}}
        if path == f"/{waba_id}/message_templates":
            return {
                "data": [
                    {
                        "id": meta_template_id, "name": "bienvenida", "language": "es_AR", "status": "PENDING",
                        "category": "UTILITY", "components": [{"type": "BODY", "text": "hola"}],
                        "quality_score": None, "parameter_format": None,
                    }
                ],
                "paging": {"cursors": {}},
            }
        raise AssertionError(f"path/método no esperado: {method} {path}")

    monkeypatch.setattr(templates_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr(sync_mod, "graph_fetch", _fake_graph_fetch)

    # 1. Crear (sin cabecera multimedia)
    r_create = await app_client.post(
        f"/admin/conexiones/meta/{connection_id}/plantillas/nueva", headers={"Cookie": cookie},
        data={
            "name": "bienvenida", "language": "es_AR", "category": "UTILITY",
            "header_format": "NONE", "body_text": "hola",
        },
    )
    assert r_create.status_code == 303
    assert "danger" not in r_create.headers["location"], r_create.headers["location"]

    # 2. Listar dispara sync -- idempotente si se pide dos veces seguidas.
    r_list_1 = await app_client.get(f"/admin/conexiones/meta/{connection_id}/plantillas", headers={"Cookie": cookie})
    assert r_list_1.status_code == 200

    from core.db.models import MessageTemplate
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(MessageTemplate).where(MessageTemplate.name == "bienvenida", MessageTemplate.tenant_id == tid)
                )
            ).scalars().all()
    assert len(rows) == 1, "el sync no debe duplicar la fila creada localmente"
    row = rows[0]
    assert row.meta_template_id == meta_template_id

    # 3. Sync manual respeta el lock -- ya se sincronizó al listar.
    r_sync = await app_client.post(f"/admin/conexiones/meta/{connection_id}/plantillas/sync", headers={"Cookie": cookie})
    assert "danger" in r_sync.headers["location"]

    # 4. Webhook de estado actualiza el listado (fuente de verdad real).
    from core.gateway.meta_template_webhooks import handle_template_status_update

    await handle_template_status_update(
        {
            "message_template_id": meta_template_id, "event": "REJECTED",
            "rejection_info": {"reason": "SCAM", "recommendation": "Revisá el contenido del mensaje"},
        }
    )

    async with tenant_scope(tid):
        async with get_session() as session:
            updated = await session.get(MessageTemplate, row.id)
    assert updated.status == "REJECTED"
    assert updated.rejected_reason == "SCAM"
    assert updated.rejection_recommendation == "Revisá el contenido del mensaje"

    # 5. Borrado de una traducción (no de toda la plantilla).
    r_delete_translation = await app_client.post(
        f"/admin/conexiones/meta/{connection_id}/plantillas/{row.id}/eliminar-traduccion", headers={"Cookie": cookie}
    )
    assert "danger" not in r_delete_translation.headers["location"]

    async with tenant_scope(tid):
        async with get_session() as session:
            after_delete = await session.get(MessageTemplate, row.id)
    assert after_delete.status == "DELETED"

    waba_row_check = await session_get_waba(tid, waba_id)
    assert waba_row_check.message_template_limit == 250


async def session_get_waba(tid, waba_id):
    from core.db.models import WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))
            ).scalar_one()

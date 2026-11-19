"""Token BYOK en la API nueva (`/api/v1/admin/meta/byok/token/*`).

Cubre: exige sesión, extender exige owner/master, y ambas operan sobre el token
ya guardado de la conexión sin exponer secretos en la respuesta.
"""

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

APP_SECRET = "app-secret-de-prueba"
STORED_TOKEN = "token-guardado"


async def _setup_connected_tenant() -> tuple[uuid.UUID, uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import APP_SECRET_KIND, CLOUD_API_ACCESS_TOKEN_KIND
    from core.shared.tenant import tenant_scope

    slug = uniq("token-tools")
    tid, _ = await onboard_tenant(
        name=f"Tenant token tools {slug}",
        slug=slug,
        business_description="Test de token BYOK.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    app_id = f"app-{slug}"
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", meta_app_id=app_id, status="connected", origin="byok"
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
        await meta_token_store.put_token(
            connection_id, APP_SECRET_KIND, app_id, APP_SECRET, tenant_id=tid
        )
        await meta_token_store.put_token(
            connection_id, CLOUD_API_ACCESS_TOKEN_KIND, app_id, STORED_TOKEN, tenant_id=tid
        )
    return tid, connection_id, app_id


async def test_token_inspect_requiere_sesion(app_client) -> None:
    r = await app_client.post(
        "/api/v1/admin/meta/byok/token/inspect", json={"connection_id": str(uuid.uuid4())}
    )
    assert r.status_code == 401


async def test_token_extend_requiere_sesion(app_client) -> None:
    r = await app_client.post(
        "/api/v1/admin/meta/byok/token/extend", json={"connection_id": str(uuid.uuid4())}
    )
    assert r.status_code == 401


async def test_extend_exige_owner_o_master(app_client) -> None:
    """Un colaborador con acceso al negocio no puede extender el token."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid, connection_id, _app_id = await _setup_connected_tenant()

    email = f"{uniq('colaborador')}@example.test"
    async with get_session() as session:
        colaborador = Admin(
            email=email, password_hash=hash_password("clave-segura-123"), role="colaborador"
        )
        session.add(colaborador)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=colaborador.id, role="colaborador"))
        await session.commit()

    headers = await api_headers(app_client, tid, email=email, password="clave-segura-123")
    r = await app_client.post(
        "/api/v1/admin/meta/byok/token/extend",
        json={"connection_id": str(connection_id)},
        headers=headers,
    )
    assert r.status_code == 403


async def test_inspect_usa_el_token_guardado_sin_exponerlo(app_client, monkeypatch) -> None:
    import core.gateway.meta_token_tools as tools_mod

    tid, connection_id, app_id = await _setup_connected_tenant()
    headers = await api_headers(app_client, tid)

    visto: dict[str, str | None] = {}

    async def _fake_inspect(app_id_arg, app_secret, token, *, version=None):
        visto.update(app_id=app_id_arg, app_secret=app_secret, token=token, version=version)
        return {
            "is_valid": True,
            "type": "USER",
            "app_id": app_id_arg,
            "expires_at": 0,
            "never_expires": True,
            "scopes": ["whatsapp_business_messaging"],
            "has_whatsapp_scopes": True,
            "user_id": "u1",
        }

    monkeypatch.setattr(tools_mod, "inspect_token", _fake_inspect)

    r = await app_client.post(
        "/api/v1/admin/meta/byok/token/inspect",
        json={"connection_id": str(connection_id)},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_valid"] is True
    assert body["never_expires"] is True
    assert body["has_whatsapp_scopes"] is True
    assert visto["app_id"] == app_id
    assert visto["app_secret"] == APP_SECRET
    assert visto["token"] == STORED_TOKEN
    assert APP_SECRET not in r.text
    assert STORED_TOKEN not in r.text


async def test_extend_persiste_el_token_nuevo_y_la_caducidad(app_client, monkeypatch) -> None:
    import core.gateway.meta_token_tools as tools_mod
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import CLOUD_API_ACCESS_TOKEN_KIND
    from core.shared.tenant import tenant_scope

    tid, connection_id, app_id = await _setup_connected_tenant()
    headers = await api_headers(app_client, tid)

    async def _fake_extend(app_id_arg, app_secret, token, *, version=None):
        assert token == STORED_TOKEN
        assert app_secret == APP_SECRET
        return {"access_token": "token-extendido-60d", "expires_in": 5_183_944}

    monkeypatch.setattr(tools_mod, "extend_token", _fake_extend)

    r = await app_client.post(
        "/api/v1/admin/meta/byok/token/extend",
        json={"connection_id": str(connection_id)},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["token_expires_at"] is not None
    assert r.json()["expires_in"] == 5_183_944
    assert "token-extendido" not in r.text

    async with tenant_scope(tid):
        guardado = await meta_token_store.get_token(
            connection_id, CLOUD_API_ACCESS_TOKEN_KIND, app_id, tenant_id=tid
        )
        caduca = await meta_token_store.get_token_expires_at(
            connection_id, CLOUD_API_ACCESS_TOKEN_KIND, app_id, tenant_id=tid
        )
    assert guardado == "token-extendido-60d"
    assert caduca is not None

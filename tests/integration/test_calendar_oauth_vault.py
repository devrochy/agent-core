"""Vault de la conexión de Google Calendar (ADR-0070).

`saves_tokens` escribe la conexión del tenant; `get_access_token` la lee y cae a
la copia vieja en `plugin_configs` durante la ventana de compatibilidad.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _cleanup(tenant_id: uuid.UUID) -> None:
    from sqlalchemy import delete

    from core.db.models import ChannelConnection, PluginConfig
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(
                delete(ChannelConnection).where(ChannelConnection.tenant_id == tenant_id)
            )
            await session.execute(
                delete(PluginConfig).where(
                    PluginConfig.tenant_id == tenant_id,
                    PluginConfig.plugin_id == "pack_calendar",
                )
            )


async def test_save_tokens_escribe_la_conexion_y_get_access_token_lee_de_ella(monkeypatch):
    from pack_calendar import oauth

    from core.gateway.connections import get_active_connection
    from core.shared.tenant import tenant_scope

    tenant_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    email = f"cal-{suffix}@ejemplo.com"

    async def _fake_email(_access_token: str) -> str:
        return email

    monkeypatch.setattr(oauth, "_account_email", _fake_email)

    tokens = {
        "access_token": f"at-{suffix}",
        "refresh_token": f"rt-{suffix}",
        "expires_at": time.time() + 3600,
    }
    try:
        async with tenant_scope(tenant_id):
            await oauth.save_tokens(dict(tokens))

            conn = await get_active_connection("google_calendar", tenant_id)
            assert conn is not None
            assert conn.category == "calendario"
            assert conn.provider == "google"
            assert conn.external_id == email
            assert conn.is_primary is False, "calendario no es mensajería"

            assert await oauth.get_access_token("cid", "secret") == f"at-{suffix}"
    finally:
        await _cleanup(tenant_id)


async def test_get_access_token_cae_a_plugin_config_sin_conexion():
    from pack_calendar import oauth

    from core.db.models import PluginConfig
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    tenant_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    try:
        async with tenant_scope(tenant_id):
            box = get_secret_box()
            async with get_session() as session:
                session.add(
                    PluginConfig(
                        plugin_id="pack_calendar",
                        tenant_id=tenant_id,
                        config_encrypted=box.encrypt_json(
                            {
                                "_oauth_tokens": {
                                    "access_token": f"old-{suffix}",
                                    "refresh_token": f"oldrt-{suffix}",
                                    "expires_at": time.time() + 3600,
                                }
                            }
                        ),
                    )
                )

            assert await oauth.get_access_token("cid", "secret") == f"old-{suffix}"
    finally:
        await _cleanup(tenant_id)


@pytest.mark.aislamiento  # subconjunto de las PRs a develop (ADR-0120)
async def test_tokens_no_se_filtran_entre_tenants(monkeypatch):
    from pack_calendar import oauth

    from core.gateway.connections import resolve_connection_credentials
    from core.shared.exceptions import PluginError
    from core.shared.tenant import tenant_scope

    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]

    async def _fake_email(_access_token: str) -> str:
        return f"a-{suffix}@ejemplo.com"

    monkeypatch.setattr(oauth, "_account_email", _fake_email)
    try:
        async with tenant_scope(tenant_a):
            await oauth.save_tokens(
                {
                    "access_token": f"at-a-{suffix}",
                    "refresh_token": f"rt-a-{suffix}",
                    "expires_at": time.time() + 3600,
                }
            )

        async with tenant_scope(tenant_b):
            # B no ve las credenciales de A y no puede pedir un token.
            assert await resolve_connection_credentials("google_calendar", tenant_b) is None
            with pytest.raises(PluginError):
                await oauth.get_access_token("cid", "secret")
    finally:
        await _cleanup(tenant_a)
        await _cleanup(tenant_b)


async def test_alta_sin_email_no_guarda_conexion(monkeypatch):
    from pack_calendar import oauth

    from core.gateway.connections import get_active_connection
    from core.shared.exceptions import PluginError
    from core.shared.tenant import tenant_scope

    tenant_id = uuid.uuid4()

    async def _empty_email(_access_token: str) -> str:
        return ""

    monkeypatch.setattr(oauth, "_account_email", _empty_email)
    try:
        async with tenant_scope(tenant_id):
            with pytest.raises(PluginError):
                await oauth.save_tokens(
                    {"access_token": "at-x", "expires_at": time.time() + 3600}
                )
            assert await get_active_connection("google_calendar", tenant_id) is None
    finally:
        await _cleanup(tenant_id)
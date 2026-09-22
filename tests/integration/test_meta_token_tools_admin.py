"""Rutas del asistente de token BYOK: exigen sesión de admin."""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_PAYLOAD = {"app_id": "a", "app_secret": "b", "access_token": "c"}


async def test_token_inspect_requiere_auth(app_client) -> None:
    r = await app_client.post(
        "/admin/conexiones/wizard/whatsapp-byok/token/inspect", data=_PAYLOAD
    )
    assert r.status_code == 401


async def test_token_extend_requiere_auth(app_client) -> None:
    r = await app_client.post(
        "/admin/conexiones/wizard/whatsapp-byok/token/extend", data=_PAYLOAD
    )
    assert r.status_code == 401

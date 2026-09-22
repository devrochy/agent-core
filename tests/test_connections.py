"""`connections.verify_meta_credentials`: mensajes claros para tokens vencidos."""

from __future__ import annotations

import pytest

from core.gateway.connections import verify_meta_credentials
from core.gateway.meta_errors import GraphError


@pytest.mark.asyncio
async def test_verify_meta_credentials_token_vencido_mensaje_claro(monkeypatch):
    async def _fail(*a, **k):
        raise GraphError(
            "Error validating access token: Session has expired",
            category="AUTH",
            code=190,
            error_subcode=463,
        )

    monkeypatch.setattr("core.gateway.meta_graph.graph_fetch", _fail)

    ok, msg, info = await verify_meta_credentials("phone", "token", "secret")
    assert ok is False
    assert info == {}
    assert "vencido" in msg.lower() or "inválido" in msg.lower()


@pytest.mark.asyncio
async def test_verify_meta_credentials_error_generico(monkeypatch):
    async def _fail(*a, **k):
        raise GraphError("otro error de Meta", category="PERMANENT", code=100)

    monkeypatch.setattr("core.gateway.meta_graph.graph_fetch", _fail)

    ok, msg, _info = await verify_meta_credentials("phone", "token", "secret")
    assert ok is False
    assert "Meta rechazó las credenciales" in msg

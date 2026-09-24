"""Extensión del core para roles de colaborador aportados por packs (ADR-0066)."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from core.mcp.types import BaseModule

if TYPE_CHECKING:
    from core.mcp.host import MCPHost


class _Modulo(BaseModule):
    id = "demo"

    def on_activate(self, host: MCPHost) -> None:
        return None


def test_colaborador_roles_default_vacio() -> None:
    assert _Modulo().collaborator_roles == []


def test_on_collaborator_role_default_no_op() -> None:
    """El hook es opcional: el default no hace nada (no rompe al core)."""
    asyncio.run(_Modulo().on_collaborator_role(uuid.uuid4(), uuid.uuid4(), "gestor"))

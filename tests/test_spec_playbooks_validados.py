"""Los playbooks que el loader suma al spec quedan como `PlaybookRef`, no como dicts.

`_ensure_default_playbooks` usaba `model_copy(update=...)`, que no valida: el spec
devuelto traía dicts y el primer turno se caía en `load_playbook`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from agentcore_contracts import AgentSpec
from agentcore_contracts.contracts import PlaybookRef

import core.agents.spec as spec_mod


@pytest.mark.asyncio
async def test_los_playbooks_agregados_son_playbookref(monkeypatch) -> None:
    @asynccontextmanager
    async def _sesion():
        yield SimpleNamespace(execute=_execute)

    async def _execute(*_a, **_k):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    monkeypatch.setattr(spec_mod, "get_session", _sesion)
    data = dict(spec_mod.DEFAULT_SPEC)
    data["tenantId"] = str(uuid.uuid4())
    data["playbooks"] = [spec_mod.DEFAULT_SPEC["playbooks"][0]]  # le faltan los demás
    spec = AgentSpec.model_validate(data)

    merged = await spec_mod._ensure_default_playbooks(uuid.uuid4(), spec)

    assert len(merged.playbooks) > 1
    assert all(isinstance(p, PlaybookRef) for p in merged.playbooks)
    assert {p.name for p in merged.playbooks} >= {r["name"] for r in spec_mod.DEFAULT_PLAYBOOK_REFS}

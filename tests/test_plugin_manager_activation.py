"""Activación de plugins: registro en mcp_host es global (una vez por proceso),
pero la config de cada tenant se valida en CADA `activate()` (ROADMAP: deuda de
`PluginManager.module.config` cacheado sin scope de tenant, cerrada 2026-09-11).

Sin mocks de BD: se monkeypatchea `load_config`/`_run_plugin_migrations`/
`_set_status` directamente sobre la instancia, evitando get_session().
"""

from __future__ import annotations

import pytest

import core.plugin_manager as plugin_manager_mod
from core.mcp.host import MCPHost
from core.mcp.types import BaseModule
from core.plugin_manager import PluginManager
from core.shared.exceptions import PluginError


@pytest.fixture(autouse=True)
def _fresh_mcp_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp_host es un singleton global real (core/mcp/host.py) -- sin esto,
    "fake_skill" quedaría registrada de un test al siguiente y el segundo
    activate() de cada test rompería con "Skill duplicada" por una razón
    ajena a lo que el test prueba."""
    monkeypatch.setattr(plugin_manager_mod, "mcp_host", MCPHost())


class _FakeModule(BaseModule):
    id = "fake_pack"
    name = "Fake Pack"
    requires_config = True

    def __init__(self) -> None:
        super().__init__()
        self.activate_calls = 0

    def on_activate(self, host) -> None:  # type: ignore[no-untyped-def]
        self.activate_calls += 1
        host.register_skill(_FakeSkill())


class _FakeSkill:
    name = "fake_skill"

    def get_tools(self):
        return []


def _manager_with_module() -> tuple[PluginManager, _FakeModule]:
    manager = PluginManager()
    module = _FakeModule()
    manager._modules[module.id] = module
    manager._run_plugin_migrations = lambda plugin_id: None  # type: ignore[method-assign]

    async def _noop_set_status(*args, **kwargs):
        return None

    manager._set_status = _noop_set_status  # type: ignore[method-assign]
    return manager, module


@pytest.mark.asyncio
async def test_second_activate_does_not_rerun_on_activate_nor_crash():
    """Antes de este fix, la segunda activación de un tenant distinto era un
    no-op total (ni siquiera cargaba su config). Ahora: on_activate() sigue
    corriendo una sola vez (si no, mcp_host.register_skill revienta con
    "Skill duplicada"), pero load_config() SIEMPRE corre."""
    manager, module = _manager_with_module()
    calls = []

    async def _fake_load_config(plugin_id: str):
        calls.append(plugin_id)
        return None

    manager.load_config = _fake_load_config  # type: ignore[method-assign]

    await manager.activate("fake_pack")
    assert module.activate_calls == 1
    assert calls == ["fake_pack"]

    # Segundo tenant activa el mismo plugin: on_activate() NO debe volver a
    # correr (rompería con "Skill duplicada"), pero load_config() sí.
    await manager.activate("fake_pack")
    assert module.activate_calls == 1, "on_activate() no debe re-correr: mcp_host es global"
    assert calls == ["fake_pack", "fake_pack"], "load_config() debe validar la config de CADA tenant"


@pytest.mark.asyncio
async def test_second_activate_surfaces_bad_config_without_touching_global_status():
    """Config inválida del segundo tenant no debe marcar el plugin como roto
    globalmente (el primer tenant lo sigue usando bien)."""
    manager, module = _manager_with_module()

    call_count = {"n": 0}

    async def _fake_load_config(plugin_id: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        raise ValueError("config del tenant B es inválida")

    manager.load_config = _fake_load_config  # type: ignore[method-assign]

    await manager.activate("fake_pack")
    assert module.activate_calls == 1

    with pytest.raises(PluginError):
        await manager.activate("fake_pack")

    # on_activate() no debió re-correr pese al error de B
    assert module.activate_calls == 1

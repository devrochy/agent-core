"""`auto_disable_for_tenant` (ADR-0024): acotar la desactivación automática
al tenant que causó el error, en vez de apagar el plugin para todos.

Incidente real (tenant `yopoldo`, 2026-09-11 y 2026-09-12, ver
`docs/ROADMAP.md` filas 143-149): una excepción de runtime causada por UN
tenant apagaba `pack_commerce`/`pack_payments` para TODOS los tenants del
proceso (`auto_disable` -> `deactivate` -> `mcp_host.remove_plugin` +
`PluginRecord.status = "error"`, ninguno de los dos con scope de tenant).

Sin mocks de BD: se monkeypatchea `_write_tenant_error`/`auto_disable`/
`record_audit` directamente sobre la instancia o el módulo, evitando
`get_session()` -- mismo criterio que `test_plugin_manager_activation.py`.
"""

from __future__ import annotations

import uuid

import pytest

import core.plugin_manager as plugin_manager_mod
from core.mcp.host import MCPHost
from core.plugin_manager import PluginManager


@pytest.fixture(autouse=True)
def _fresh_mcp_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_manager_mod, "mcp_host", MCPHost())


@pytest.fixture(autouse=True)
def _no_real_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """`record_audit` es best-effort (fail-open) contra una BD real, pero
    en la suite pura no hay BD -- se monkeypatchea para no depender de una
    conexión real ni de su timing."""
    calls: list[dict] = []

    async def _fake_record_audit(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(plugin_manager_mod, "record_audit", _fake_record_audit)
    return calls


def _manager() -> PluginManager:
    return PluginManager()


@pytest.mark.asyncio
async def test_auto_disable_for_tenant_no_toca_mcp_host_ni_deactivate(monkeypatch: pytest.MonkeyPatch):
    """Con un tenant real: se escribe el error POR TENANT, sin llamar
    `deactivate()` ni tocar `mcp_host` -- otros tenants no se enteran."""
    manager = _manager()
    written = []

    async def _fake_write(tenant_id, plugin_id, reason):
        written.append((tenant_id, plugin_id, reason))

    deactivate_calls = []

    async def _fake_deactivate(*args, **kwargs):
        deactivate_calls.append((args, kwargs))

    monkeypatch.setattr(manager, "_write_tenant_error", _fake_write)
    monkeypatch.setattr(manager, "deactivate", _fake_deactivate)

    tenant_id = uuid.uuid4()
    await manager.auto_disable_for_tenant(tenant_id, "pack_commerce", "boom")

    assert written == [(tenant_id, "pack_commerce", "boom")]
    assert deactivate_calls == [], "no debe llamar deactivate() -- eso apagaría mcp_host para todos"


@pytest.mark.asyncio
async def test_auto_disable_for_tenant_sin_tenant_cae_al_global(monkeypatch: pytest.MonkeyPatch):
    """Sin tenant resuelto (contexto de sistema, ej. un cron): no hay dónde
    acotar el daño, así que cae al `auto_disable()` global de siempre."""
    manager = _manager()
    global_calls = []

    async def _fake_auto_disable(plugin_id, reason):
        global_calls.append((plugin_id, reason))

    written = []

    async def _fake_write(tenant_id, plugin_id, reason):
        written.append((tenant_id, plugin_id, reason))

    monkeypatch.setattr(manager, "auto_disable", _fake_auto_disable)
    monkeypatch.setattr(manager, "_write_tenant_error", _fake_write)

    await manager.auto_disable_for_tenant(None, "pack_commerce", "boom")

    assert global_calls == [("pack_commerce", "boom")]
    assert written == [], "sin tenant no debe escribir en plugin_tenant_state"


@pytest.mark.asyncio
async def test_auto_disable_for_tenant_registra_auditoria(monkeypatch: pytest.MonkeyPatch, _no_real_audit):
    manager = _manager()

    async def _fake_write(tenant_id, plugin_id, reason):
        return None

    monkeypatch.setattr(manager, "_write_tenant_error", _fake_write)

    tenant_id = uuid.uuid4()
    await manager.auto_disable_for_tenant(tenant_id, "pack_commerce", "boom")

    assert len(_no_real_audit) == 1
    assert _no_real_audit[0]["actor"] == "plugin:pack_commerce"
    assert _no_real_audit[0]["action"] == "auto_disabled_for_tenant"

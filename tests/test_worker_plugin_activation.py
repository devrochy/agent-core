"""HU-SUB-03 / ADR-0107: el worker de Celery registra los suscriptores de los packs.

Antes solo la API (`core/main.py`) activaba los packs; el despachador del outbox
corría en el worker con `event_bus` vacío y marcaba `entregado` sin ejecutar ningún
handler. Estos tests no suscriben a mano: pasan por `activate`/`ensure_process_activation`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.tasks.domain_events_dispatch as dispatch_mod
from core.mcp.host import mcp_host
from core.mcp.types import BaseModule
from core.plugin_manager import PluginManager
from core.shared.events import event_bus

TOPIC = "test.worker_activation"


class _PackFalso(BaseModule):
    id = "pack_falso_worker"
    name = "Pack falso"
    requires_config = False

    def __init__(self) -> None:
        self.recibidos: list[dict] = []

    async def _on_evento(self, **payload) -> None:
        self.recibidos.append(payload)

    def on_activate(self, host) -> None:
        host.subscribe(TOPIC, self._on_evento)


@pytest.fixture
def pm(monkeypatch):
    manager = PluginManager()
    modulo = _PackFalso()
    manager._modules[modulo.id] = modulo

    async def _load_config(plugin_id):
        return None

    escrituras: list[tuple] = []

    async def _set_status(plugin_id, status, **kw):
        escrituras.append((plugin_id, status))

    migraciones: list[str] = []
    monkeypatch.setattr(manager, "load_config", _load_config)
    monkeypatch.setattr(manager, "_set_status", _set_status)
    monkeypatch.setattr(manager, "_run_plugin_migrations", migraciones.append)
    manager._escrituras = escrituras  # type: ignore[attr-defined]
    manager._migraciones = migraciones  # type: ignore[attr-defined]
    yield manager, modulo
    mcp_host.remove_plugin(modulo.id)


@pytest.mark.asyncio
async def test_register_only_suscribe_sin_tocar_estado_ni_migraciones(pm) -> None:
    manager, modulo = pm

    await manager.activate(modulo.id, register_only=True)

    assert event_bus.has_subscribers(TOPIC)
    await event_bus.emit_strict(TOPIC, ref="OC-1")
    assert modulo.recibidos == [{"ref": "OC-1"}]
    assert manager._escrituras == []
    assert manager._migraciones == []


@pytest.mark.asyncio
async def test_ensure_process_activation_activa_una_vez_por_pid(pm, monkeypatch) -> None:
    manager, modulo = pm
    llamadas: list[bool] = []

    async def _activate_all(*, register_only=False):
        llamadas.append(register_only)
        await manager.activate(modulo.id, register_only=register_only)

    monkeypatch.setattr(manager, "activate_all_configured", _activate_all)
    monkeypatch.setattr("core.plugin_manager.os.getpid", lambda: 100)

    await manager.ensure_process_activation()
    await manager.ensure_process_activation()
    assert llamadas == [True]

    # Proceso hijo (fork): vuelve a activar sin duplicar el handler heredado.
    monkeypatch.setattr("core.plugin_manager.os.getpid", lambda: 200)
    await manager.ensure_process_activation()
    assert llamadas == [True, True]
    await event_bus.emit_strict(TOPIC, ref="OC-2")
    assert modulo.recibidos == [{"ref": "OC-2"}]


@pytest.mark.asyncio
async def test_el_despachador_activa_los_plugins_del_proceso(monkeypatch) -> None:
    orden: list[str] = []

    async def _ensure():
        orden.append("activar")

    async def _dispatch(limit=dispatch_mod.BATCH):
        orden.append("despachar")
        return 0

    monkeypatch.setattr(
        "core.plugin_manager.plugin_manager", SimpleNamespace(ensure_process_activation=_ensure)
    )
    monkeypatch.setattr(dispatch_mod, "dispatch_due_events", _dispatch)

    await dispatch_mod._run_dispatch()
    assert orden == ["activar", "despachar"]

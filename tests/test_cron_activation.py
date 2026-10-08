"""`register_cron` activa los plugins del proceso antes de chequear `is_active`.

Antes, en el worker `_active` estaba vacío (solo la API activaba packs) y TODO cron de
pack se salteaba con `cron_skipped_plugin_inactive` (visto al hacer HU-SUB-04)."""

from __future__ import annotations

from core.plugin_manager import plugin_manager
from core.tasks.celery_app import celery
from core.tasks.decorators import register_cron


def test_el_cron_activa_el_proceso_y_corre(monkeypatch) -> None:
    corrio: list[str] = []
    activado: list[bool] = []

    async def _job() -> None:
        corrio.append("si")

    register_cron("pack_cron_prueba", "0 0 * * *")(_job)

    async def _ensure() -> None:
        activado.append(True)
        plugin_manager._active.add("pack_cron_prueba")

    monkeypatch.setattr(plugin_manager, "ensure_process_activation", _ensure)
    try:
        celery.tasks["pack_cron_prueba._job"].run()
    finally:
        plugin_manager._active.discard("pack_cron_prueba")
    assert activado == [True]
    assert corrio == ["si"]

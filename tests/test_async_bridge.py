"""Regresión de los dos bugs que impedían enviar respuestas:

1. `core.outbound_send` debe quedar registrado en el worker (vía `include`),
   si no el broker lo rechaza como "unregistered task".
2. El puente sync→async de los tasks Celery debe usar UN loop persistente,
   no `asyncio.run()` por task (que rompía el engine/Redis cacheados con
   `Event loop is closed`).
"""

from __future__ import annotations

import asyncio

import pytest


def test_outbound_send_esta_en_include_de_celery() -> None:
    from core.tasks.celery_app import celery

    assert "core.tasks.outbound_send" in (celery.conf.include or ()), (
        "sin include, el worker no registra core.outbound_send y el envío nunca corre"
    )


def test_run_async_reutiliza_un_solo_loop() -> None:
    from core.shared.async_bridge import run_async

    async def _loop_id() -> int:
        return id(asyncio.get_running_loop())

    assert run_async(_loop_id()) == run_async(_loop_id())


def test_run_async_no_se_llama_desde_un_loop() -> None:
    import pytest

    from core.shared.async_bridge import run_async

    async def _inner() -> None:
        coro = asyncio.sleep(0)
        try:
            # Desde dentro de un loop corriendo debe fallar claro (no bloquearlo).
            with pytest.raises(RuntimeError, match="event loop corriendo"):
                run_async(coro)
        finally:
            coro.close()

    asyncio.run(_inner())


def test_run_async_recrea_loop_si_el_thread_murio() -> None:
    import threading

    from core.shared import async_bridge

    stale_loop = asyncio.new_event_loop()
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    async_bridge._loop = stale_loop
    async_bridge._thread = dead

    async def _loop_id() -> int:
        return id(asyncio.get_running_loop())

    try:
        assert isinstance(async_bridge.run_async(_loop_id()), int)
    finally:
        stale_loop.close()


def test_beat_scan_registra_crons_de_packs() -> None:
    """El beat debe escanear plugins (`beat_init`) para programar los crons de
    los packs; sin eso, `follow_up_leads`/`follow_up_quotes` nunca corren."""
    import importlib.util

    if importlib.util.find_spec("pack_commerce") is None:
        pytest.skip("pack_commerce no instalado")

    from core.tasks import celery_app as app

    app._load_plugins()
    assert "pack_commerce.follow_up_leads" in app.celery.conf.beat_schedule

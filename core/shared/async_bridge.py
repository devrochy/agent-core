"""Puente sync→async para procesos SIN event loop (tareas Celery).

El worker de Celery (pool prefork) ejecuta cada task en un proceso de larga
vida pero sin event loop: los tasks síncronos llaman `run_async(coro)`. Si cada
llamada usara `asyncio.run()`, se crearía un loop NUEVO por task y los
singletons async del proceso (engine de SQLAlchemy con su pool, cliente de
Redis) quedarían atados al primer loop -- ya cerrado -- y el siguiente task
fallaría con `RuntimeError: Event loop is closed`.

Este módulo corre TODAS las corrutinas en un único event loop persistente
(thread daemon), creado lazy, para que esos singletons vivan en un loop vivo
durante toda la vida del proceso.

Uso: SOLO desde contexto síncrono sin loop corriendo (tasks Celery, fallback
sync del event bus). Desde dentro de un loop, usar `await` directo.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if (
            _loop is None
            or _loop.is_closed()
            or _thread is None
            or not _thread.is_alive()
        ):
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="agent-core-async-bridge",
                daemon=True,
            )
            thread.start()
            _loop = loop
            _thread = thread
        return _loop


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Ejecuta `coro` en el loop persistente del proceso y bloquea hasta el
    resultado. Debe llamarse desde un hilo SIN loop corriendo (task Celery)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "run_async() no debe llamarse desde un event loop corriendo: usá `await` "
            "directo (bloquearía el loop del llamador)."
        )
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop()).result()

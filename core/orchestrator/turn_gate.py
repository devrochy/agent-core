"""Gate de turnos: cuota de concurrencia por tenant (Sprint 1, ADR-0006).

Un solo worker procesa turnos de todos los tenants; sin cuota, un tenant que
encola 1.000 mensajes llena la cola y ahoga a los demás. Este gate:

1. `asyncio.Semaphore` GLOBAL acotado (max_concurrent_turns): el worker corre N
   turnos en paralelo.
2. `asyncio.Semaphore` POR TENANT (max_turns_per_tenant): un tenant ocupa a lo
   sumo N slots del pool global -> el resto de tenants siempre encuentra cupo.
3. Contador Redis `turns:{tenant_id}` (INCR/DECR): observable y listo para
   multi-worker; hoy lo gobiernan los semáforos en proceso (un worker).

Todo turno (camino inmediato del consumer Y fire del debounce) pasa por
`run_turn(tenant_id, coro)`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from typing import Any

from core.config import get_settings
from core.memory.working import get_redis

_global_sem: asyncio.Semaphore | None = None
_tenant_sems: dict[str, asyncio.Semaphore] = {}


def _ensure_global() -> asyncio.Semaphore:
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(get_settings().max_concurrent_turns)
    return _global_sem


def _tenant_sem(tenant_id: uuid.UUID) -> asyncio.Semaphore:
    key = str(tenant_id)
    sem = _tenant_sems.get(key)
    if sem is None:
        sem = asyncio.Semaphore(get_settings().max_turns_per_tenant)
        _tenant_sems[key] = sem
    return sem


async def run_turn(tenant_id: uuid.UUID | str | None, coro: Awaitable[Any]) -> Any:
    """Ejecuta un turno bajo la cuota global + por tenant."""
    if tenant_id is None:
        return await coro
    tid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    r = get_redis()
    counter_key = f"turns:{tid}"
    try:
        await r.incr(counter_key)
        await r.expire(counter_key, 3600)
    except Exception:
        pass  # el contador es observabilidad; el gate real son los semáforos

    try:
        async with _ensure_global(), _tenant_sem(tid):
            return await coro
    finally:
        try:
            await r.decr(counter_key)
        except Exception:
            pass
"""Gate de turnos: cuota de concurrencia por tenant (Sprint 1, ADR-0006).

Un solo worker procesa turnos de todos los tenants; sin cuota, un tenant que
encola 1.000 mensajes llena la cola y ahoga a los demás. Este gate:

1. `asyncio.Semaphore` GLOBAL acotado (max_concurrent_turns): el worker corre N
   turnos en paralelo.
2. `asyncio.Semaphore` POR TENANT (max_turns_per_tenant): un tenant ocupa a lo
   sumo N slots del pool global -> el resto de tenants siempre encuentra cupo.
   La espera por ese cupo tiene tope (`turn_queue_timeout_s`, HU-ARCH-27): si el
   tenant está saturado más allá del tope, `TurnQueueTimeout` deja que el
   llamador reprograme/reclame en vez de esperar para siempre.
3. Presupuesto TOTAL del turno (`turn_timeout_s`, HU-ARCH-27): un turno que se
   pasa se corta con `TurnTimeout`; nunca queda colgado. Los lentos se registran
   (`turn_slow`) para verlos antes de que sean incidente.
4. Contador Redis `turns:{tenant_id}` (INCR/DECR): observable y listo para
   multi-worker; hoy lo gobiernan los semáforos en proceso (un worker).

Todo turno (camino inmediato del consumer Y fire del debounce) pasa por
`run_turn(tenant_id, coro)`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable
from typing import Any

from core.config import get_settings
from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)


class TurnQueueTimeout(RuntimeError):
    """La espera por el cupo del tenant excedió `turn_queue_timeout_s` (HU-ARCH-27).

    El turno NO se ejecutó: el llamador debe reprogramarlo o dejarlo pendiente
    para el reclamo, sin contarlo como fallo de negocio.
    """


class TurnTimeout(RuntimeError):
    """El turno excedió `turn_timeout_s` y se cortó (HU-ARCH-27)."""


# Cota simple del cache de semáforos por tenant: un proceso de larga vida no
# debe acumular un entry para siempre por cada tenant_id distinto que haya
# visto alguna vez (deuda registrada en el ROADMAP). Solo se evictan tenants
# sin un turno activo/en cola ahora mismo -- _active_tenants es la guarda.
_TENANT_SEM_CACHE_MAX = 500

_global_sem: asyncio.Semaphore | None = None
_tenant_sems: OrderedDict[str, asyncio.Semaphore] = OrderedDict()
# Refcount, no set: dos turnos concurrentes del mismo tenant no deben
# pisarse -- el segundo turno en terminar es el que debe limpiar la marca.
_active_tenants: dict[str, int] = {}


def _ensure_global() -> asyncio.Semaphore:
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(get_settings().max_concurrent_turns)
    return _global_sem


def _tenant_sem(tenant_id: uuid.UUID) -> asyncio.Semaphore:
    key = str(tenant_id)
    sem = _tenant_sems.get(key)
    if sem is not None:
        _tenant_sems.move_to_end(key)
        return sem
    sem = asyncio.Semaphore(get_settings().max_turns_per_tenant)
    _tenant_sems[key] = sem
    _evict_idle()
    return sem


def _evict_idle() -> None:
    """Poda entries LRU por encima de la cota, salteando tenants con un
    turno activo ahora (nunca se evicta un semáforo que alguien está
    esperando o sosteniendo)."""
    if len(_tenant_sems) <= _TENANT_SEM_CACHE_MAX:
        return
    for key in list(_tenant_sems.keys()):
        if len(_tenant_sems) <= _TENANT_SEM_CACHE_MAX:
            return
        if key in _active_tenants:
            continue
        del _tenant_sems[key]


async def run_turn(tenant_id: uuid.UUID | str | None, coro: Awaitable[Any]) -> Any:
    """Ejecuta un turno bajo la cuota global + por tenant, con presupuesto acotado.

    HU-ARCH-27: la espera por el cupo del tenant y la duración total del turno
    tienen tope; `TurnQueueTimeout`/`TurnTimeout` distinguen "no llegó a correr"
    de "se pasó de tiempo" para que el llamador decida (reprogramar vs reintentar).
    """
    if tenant_id is None:
        return await coro
    tid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    settings = get_settings()
    r = get_redis()
    counter_key = f"turns:{tid}"
    try:
        await r.incr(counter_key)
        await r.expire(counter_key, 3600)
    except Exception:
        pass  # el contador es observabilidad; el gate real son los semáforos

    key = str(tid)
    started = time.monotonic()
    _active_tenants[key] = _active_tenants.get(key, 0) + 1
    try:
        async with _ensure_global():
            sem = _tenant_sem(tid)
            try:
                await asyncio.wait_for(sem.acquire(), timeout=settings.turn_queue_timeout_s)
            except TimeoutError as exc:
                log.warning(
                    "turn_queue_timeout",
                    tenant_id=key,
                    wait_s=settings.turn_queue_timeout_s,
                )
                raise TurnQueueTimeout(
                    f"cupo del tenant saturado por más de {settings.turn_queue_timeout_s}s"
                ) from exc
            try:
                waited = time.monotonic() - started
                if waited >= settings.turn_slow_warn_s:
                    log.warning("turn_slow_wait", tenant_id=key, wait_s=round(waited, 1))
                try:
                    return await asyncio.wait_for(coro, timeout=settings.turn_timeout_s)
                except TimeoutError as exc:
                    duration = time.monotonic() - started
                    log.error(
                        "turn_timeout",
                        tenant_id=key,
                        duration_s=round(duration, 1),
                        timeout_s=settings.turn_timeout_s,
                    )
                    raise TurnTimeout(
                        f"turno cortado a los {settings.turn_timeout_s}s"
                    ) from exc
            finally:
                sem.release()
    finally:
        duration = time.monotonic() - started
        if duration >= settings.turn_slow_warn_s:
            log.warning("turn_slow", tenant_id=key, duration_s=round(duration, 1))
        remaining = _active_tenants.get(key, 1) - 1
        if remaining <= 0:
            _active_tenants.pop(key, None)
        else:
            _active_tenants[key] = remaining
        try:
            await r.decr(counter_key)
        except Exception:
            pass
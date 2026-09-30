"""Tope diario de salida y kill-switch del canal `waweb` (ADR-0074).

El tope y la pausa son la mitigación anti-baneo que **se ejecuta de verdad**: el
connector consulta `allow_send` antes de cada envío y lleva el contador del día en
Redis. El kill-switch permite cortar los envíos (global o por tenant) sin tocar
la sesión.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from core.memory.working import get_redis
from core.waweb.policy import WaWebPolicy

_CAP_PREFIX = "waweb:cap:"
_PAUSE_GLOBAL = "waweb:sends_paused"
_PAUSE_TENANT_PREFIX = "waweb:sends_paused:"
_DISCONNECT_PREFIX = "waweb:disconnects:"
_DISCONNECT_TTL_S = 600
DISCONNECT_ALERT_THRESHOLD = 5
_TRUTHY = frozenset({"1", "true", "yes", "on", "si", "sí"})
_CAP_TTL_S = 2 * 24 * 3600


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def cap_key(tenant_id: uuid.UUID) -> str:
    return f"{_CAP_PREFIX}{tenant_id}:{_today()}"


def tenant_pause_key(tenant_id: uuid.UUID) -> str:
    return f"{_PAUSE_TENANT_PREFIX}{tenant_id}"


def _is_true(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


async def is_paused(tenant_id: uuid.UUID) -> bool:
    r = get_redis()
    if _is_true(await r.get(_PAUSE_GLOBAL)):
        return True
    return _is_true(await r.get(tenant_pause_key(tenant_id)))


async def outbound_count(tenant_id: uuid.UUID) -> int:
    raw = await get_redis().get(cap_key(tenant_id))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def allow_send(tenant_id: uuid.UUID, policy: WaWebPolicy) -> bool:
    """True si el envío está permitido (no pausado y bajo el tope del día)."""
    if policy.daily_outbound_cap <= 0:
        return False
    if await is_paused(tenant_id):
        return False
    return await outbound_count(tenant_id) < policy.daily_outbound_cap


async def record_outbound(tenant_id: uuid.UUID) -> None:
    """Cuenta un envío del día (crea la clave con TTL de 2 días)."""
    r = get_redis()
    key = cap_key(tenant_id)
    value = await r.incr(key)
    if value == 1:
        await r.expire(key, _CAP_TTL_S)


def disconnect_key(tenant_id: uuid.UUID) -> str:
    return f"{_DISCONNECT_PREFIX}{tenant_id}"


async def record_disconnect(tenant_id: uuid.UUID) -> int:
    """Cuenta una desconexión reciente; devuelve el acumulado (ventana 10 min)."""
    r = get_redis()
    key = disconnect_key(tenant_id)
    value = await r.incr(key)
    if value == 1:
        await r.expire(key, _DISCONNECT_TTL_S)
    return int(value)


async def clear_disconnects(tenant_id: uuid.UUID) -> None:
    await get_redis().delete(disconnect_key(tenant_id))

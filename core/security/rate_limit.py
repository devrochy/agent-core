"""Rate limit de ventana deslizante en Redis, compartido entre instancias.

Cada cupo (`Bucket`) es un sorted set con un miembro por hit (score = instante).
En cada operación se purgan los hits fuera de la ventana, el set se recorta a lo
justo para decidir (una inundación no lo infla) y la clave lleva TTL = ventana,
así que una clave que deja de recibir hits se borra sola.

Operaciones:

- `hit(bucket, guards=..., known_key=...)`: suma un hit a `bucket` y dice si hay
  que cortar: si `bucket` superó su cupo, o si alguno de los `guards` ya está
  lleno. Los guards solo se leen; se llenan con `record`. Si `known_key` existe
  en Redis (ver `remember`), los guards no aplican. Todo va en un pipeline
  transaccional, un solo viaje a Redis.
- `record(bucket)`: suma un hit sin decidir nada. Sirve para contar fallos
  (p. ej. un 404 por una clave inexistente) en un guard que luego consulta `hit`.
- `remember(known_key, ttl_s)`: marca algo como conocido (p. ej. una clave que
  sí existe), para que un guard lleno de fallos ajenos no lo corte.

Si Redis no responde, deja pasar (fail-open): el límite es una barrera
complementaria y una caída de Redis no debe tumbar la ruta que protege.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from redis.exceptions import RedisError

from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

_clock = time.time


@dataclass(frozen=True)
class Bucket:
    """Un cupo: como mucho `limit` hits por `window_s` segundos en `key`."""

    key: str
    limit: int
    window_s: int


def _member(now: float) -> str:
    return f"{now:.6f}:{uuid.uuid4().hex}"


async def hit(
    bucket: Bucket,
    *,
    guards: Sequence[Bucket] = (),
    known_key: str | None = None,
    name: str,
) -> bool:
    """Suma un hit a `bucket`. True si superó su cupo o si un guard está lleno
    (los guards no aplican si `known_key` existe).

    Los hits rechazados también cuentan. `name` identifica la ruta en el log.
    """
    now = _clock()
    try:
        async with get_redis().pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(bucket.key, "-inf", now - bucket.window_s)
            pipe.zadd(bucket.key, {_member(now): now})
            # Alcanza con guardar los `limit + 1` hits más recientes para decidir.
            pipe.zremrangebyrank(bucket.key, 0, -(bucket.limit + 2))
            pipe.zcard(bucket.key)
            pipe.expire(bucket.key, bucket.window_s)
            if known_key is not None:
                pipe.exists(known_key)
            for guard in guards:
                # Solo lectura: los hits fuera de la ventana no cuentan aunque
                # sigan en el set (los purga el próximo `record`).
                pipe.zcount(guard.key, f"({now - guard.window_s}", "+inf")
            results = await pipe.execute()
    except RedisError as exc:
        log.warning("rate_limit_unavailable", name=name, error=str(exc))
        return False
    if int(results[3]) > bucket.limit:
        return True
    counts = results[5:]
    if known_key is not None:
        if int(counts[0]):
            return False
        counts = counts[1:]
    return any(int(count) >= guard.limit for guard, count in zip(guards, counts, strict=True))


async def record(bucket: Bucket, *, name: str) -> None:
    """Suma un hit a `bucket` sin decidir nada (lo consulta `hit` como guard)."""
    now = _clock()
    try:
        async with get_redis().pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(bucket.key, "-inf", now - bucket.window_s)
            pipe.zadd(bucket.key, {_member(now): now})
            # Un guard está lleno con `limit` hits: no hace falta guardar más.
            pipe.zremrangebyrank(bucket.key, 0, -(bucket.limit + 1))
            pipe.expire(bucket.key, bucket.window_s)
            await pipe.execute()
    except RedisError as exc:
        log.warning("rate_limit_unavailable", name=name, error=str(exc))


async def remember(known_key: str, ttl_s: int, *, name: str) -> None:
    """Marca `known_key` como conocida por `ttl_s` segundos (ver `hit`)."""
    try:
        await get_redis().set(known_key, "1", ex=ttl_s)
    except RedisError as exc:
        log.warning("rate_limit_unavailable", name=name, error=str(exc))

"""Rate limit de ventana deslizante en Redis (`core.security.rate_limit`).

Lo comparten el widget del chat web y el intake de leads de pack-commerce; los
tests de cada ruta cubren cómo arman sus claves. Redis es el doble de
`tests/conftest.py` (`rl_redis`, `rl_clock`).
"""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from core.security import rate_limit
from core.security.rate_limit import Bucket

B = Bucket("rl:test:k", limit=3, window_s=60)
GUARD = Bucket("rl:test:guard", limit=2, window_s=60)


async def test_corta_tras_el_tope(rl_redis) -> None:
    assert not await rate_limit.hit(B, name="t")
    assert not await rate_limit.hit(B, name="t")
    assert not await rate_limit.hit(B, name="t")
    assert await rate_limit.hit(B, name="t")  # cuarto supera el tope de 3


async def test_la_ventana_desliza(rl_redis, rl_clock) -> None:
    for _ in range(3):
        await rate_limit.hit(B, name="t")
    rl_clock.now += B.window_s - 1
    assert await rate_limit.hit(B, name="t")  # todavía en la ventana

    rl_clock.now += B.window_s
    assert not await rate_limit.hit(B, name="t")  # los hits viejos se purgaron
    assert len(rl_redis.zsets[B.key]) == 1


async def test_claves_inactivas_se_purgan(rl_redis, rl_clock) -> None:
    for i in range(50):
        await rate_limit.hit(Bucket(f"rl:test:{i}", 3, 60), name="t")
    await rate_limit.record(GUARD, name="t")
    assert len(rl_redis.keys()) == 51

    rl_clock.now += 60
    assert rl_redis.keys() == set()


async def test_una_inundacion_no_infla_el_set(rl_redis) -> None:
    for _ in range(500):
        await rate_limit.hit(B, name="t")
    assert await rate_limit.hit(B, name="t")
    assert len(rl_redis.zsets[B.key]) == B.limit + 1


async def test_record_guarda_como_mucho_el_cupo(rl_redis) -> None:
    for _ in range(50):
        await rate_limit.record(GUARD, name="t")
    assert len(rl_redis.zsets[GUARD.key]) == GUARD.limit


async def test_guard_lleno_corta_aunque_el_cupo_propio_este_libre(rl_redis) -> None:
    await rate_limit.record(GUARD, name="t")
    assert not await rate_limit.hit(B, guards=(GUARD,), name="t")  # 1 de 2: no está lleno
    await rate_limit.record(GUARD, name="t")
    assert await rate_limit.hit(B, guards=(GUARD,), name="t")


async def test_hit_solo_lee_el_guard(rl_redis) -> None:
    for _ in range(10):
        await rate_limit.hit(Bucket("rl:test:otro", 100, 60), guards=(GUARD,), name="t")
    assert GUARD.key not in rl_redis.keys()


async def test_guard_vencido_no_cuenta(rl_redis, rl_clock) -> None:
    for _ in range(GUARD.limit):
        await rate_limit.record(GUARD, name="t")
    assert await rate_limit.hit(B, guards=(GUARD,), name="t")

    rl_clock.now += GUARD.window_s
    assert not await rate_limit.hit(B, guards=(GUARD,), name="t")


KNOWN = "rl:test:known:k"


async def test_known_key_salta_el_guard_lleno(rl_redis) -> None:
    for _ in range(GUARD.limit):
        await rate_limit.record(GUARD, name="t")
    assert await rate_limit.hit(B, guards=(GUARD,), known_key=KNOWN, name="t")  # no conocida

    await rate_limit.remember(KNOWN, 900, name="t")
    assert not await rate_limit.hit(B, guards=(GUARD,), known_key=KNOWN, name="t")


async def test_known_key_no_salta_el_cupo_propio(rl_redis) -> None:
    await rate_limit.remember(KNOWN, 900, name="t")
    for _ in range(B.limit):
        assert not await rate_limit.hit(B, guards=(GUARD,), known_key=KNOWN, name="t")
    assert await rate_limit.hit(B, guards=(GUARD,), known_key=KNOWN, name="t")


async def test_known_key_vence(rl_redis, rl_clock) -> None:
    await rate_limit.remember(KNOWN, 900, name="t")
    assert KNOWN in rl_redis.keys()
    rl_clock.now += 900
    assert KNOWN not in rl_redis.keys()


class _Down:
    def pipeline(self, transaction: bool = True):
        raise RedisConnectionError("down")


async def test_redis_caido_deja_pasar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "get_redis", lambda: _Down())
    assert not await rate_limit.hit(B, guards=(GUARD,), known_key=KNOWN, name="t")
    await rate_limit.record(GUARD, name="t")  # no levanta


async def test_remember_con_redis_caido_no_levanta(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DownSet:
        async def set(self, *args, **kwargs):
            raise RedisConnectionError("down")

    monkeypatch.setattr(rate_limit, "get_redis", lambda: _DownSet())
    await rate_limit.remember(KNOWN, 900, name="t")

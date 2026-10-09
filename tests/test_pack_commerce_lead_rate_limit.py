"""Rate limit del intake público de leads (`POST /api/pack/pack_commerce/leads/{slug}`).

El cupo es por (tenant, IP): una IP que manda leads a un tenant no consume el
cupo de otro. Las entradas vencen solas (ventana deslizante + TTL de la clave),
así que las IPs que dejan de mandar no se acumulan.

Redis se reemplaza por un doble en memoria con reloj controlado que implementa
solo lo que usa `_rate_limited` (sorted set + `EXPIRE` en un pipeline).
"""

from __future__ import annotations

import pytest
from pack_commerce import api
from redis.exceptions import ConnectionError as RedisConnectionError


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeRedis:
    """Sorted sets con TTL, leyendo el tiempo de `clock`."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires_at: dict[str, float] = {}

    def _expire_due(self) -> None:
        for key, at in list(self.expires_at.items()):
            if at <= self.clock():
                self.zsets.pop(key, None)
                self.expires_at.pop(key, None)

    def keys(self) -> set[str]:
        self._expire_due()
        return set(self.zsets)

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def zremrangebyscore(self, key: str, lo: str, hi: float) -> _FakePipeline:
        self.ops.append(("zremrangebyscore", key, hi))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> _FakePipeline:
        self.ops.append(("zadd", key, mapping))
        return self

    def zremrangebyrank(self, key: str, start: int, stop: int) -> _FakePipeline:
        self.ops.append(("zremrangebyrank", key, start, stop))
        return self

    def zcard(self, key: str) -> _FakePipeline:
        self.ops.append(("zcard", key))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self.ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> list:
        r = self.redis
        r._expire_due()
        results: list = []
        for op, key, *args in self.ops:
            zset = r.zsets.setdefault(key, {})
            if op == "zremrangebyscore":
                before = len(zset)
                for member, score in list(zset.items()):
                    if score <= args[0]:
                        del zset[member]
                results.append(before - len(zset))
            elif op == "zadd":
                zset.update(args[0])
                results.append(len(args[0]))
            elif op == "zremrangebyrank":
                ordered = sorted(zset, key=zset.__getitem__)
                start, stop = args
                stop = len(ordered) + stop if stop < 0 else stop
                doomed = ordered[start : stop + 1] if stop >= 0 else []  # como Redis
                for member in doomed:
                    del zset[member]
                results.append(len(doomed))
            elif op == "zcard":
                results.append(len(zset))
            elif op == "expire":
                r.expires_at[key] = r.clock() + args[0]
                results.append(True)
            if not zset:
                r.zsets.pop(key, None)
        return results


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(api, "_clock", c)
    return c


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> _FakeRedis:
    fake = _FakeRedis(clock)
    monkeypatch.setattr(api, "get_redis", lambda: fake)
    monkeypatch.setattr(api, "_RATE_LIMIT", 3)
    return fake


async def test_supera_el_cupo_en_la_ventana(redis: _FakeRedis) -> None:
    ip = "203.0.113.1"
    assert not await api._rate_limited("tenant-a", ip)
    assert not await api._rate_limited("tenant-a", ip)
    assert not await api._rate_limited("tenant-a", ip)
    assert await api._rate_limited("tenant-a", ip)  # cuarta supera el tope de 3


async def test_cupo_de_un_tenant_no_afecta_a_otro(redis: _FakeRedis) -> None:
    ip = "203.0.113.1"
    for _ in range(5):
        await api._rate_limited("tenant-a", ip)
    assert await api._rate_limited("tenant-a", ip)

    # Misma IP, otro tenant: cupo intacto.
    assert not await api._rate_limited("tenant-b", ip)
    assert not await api._rate_limited("tenant-b", ip)
    assert not await api._rate_limited("tenant-b", ip)
    assert await api._rate_limited("tenant-b", ip)


async def test_la_ventana_desliza_y_libera_el_cupo(redis: _FakeRedis, clock: _Clock) -> None:
    ip = "203.0.113.1"
    for _ in range(3):
        await api._rate_limited("tenant-a", ip)
    clock.now += api._RATE_WINDOW_S - 1
    assert await api._rate_limited("tenant-a", ip)  # todavía en la ventana

    clock.now += api._RATE_WINDOW_S
    assert not await api._rate_limited("tenant-a", ip)  # los hits viejos se purgaron
    key = api._rate_key("tenant-a", ip)
    assert len(redis.zsets[key]) == 1


async def test_ips_inactivas_se_purgan(redis: _FakeRedis, clock: _Clock) -> None:
    for i in range(50):
        await api._rate_limited("tenant-a", f"198.51.100.{i}")
    assert len(redis.keys()) == 50

    clock.now += api._RATE_WINDOW_S
    assert redis.keys() == set()


async def test_una_inundacion_no_infla_el_set(redis: _FakeRedis) -> None:
    ip = "203.0.113.1"
    for _ in range(500):
        await api._rate_limited("tenant-a", ip)
    assert await api._rate_limited("tenant-a", ip)
    assert len(redis.zsets[api._rate_key("tenant-a", ip)]) == api._RATE_LIMIT + 1


async def test_slug_invalido_no_crea_claves_arbitrarias(redis: _FakeRedis) -> None:
    await api._rate_limited("../../" + "x" * 500, "203.0.113.1")
    assert redis.keys() == {f"{api._RATE_KEY_PREFIX}:_invalid:203.0.113.1"}


async def test_redis_caido_deja_pasar(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down:
        def pipeline(self, transaction: bool = True):
            raise RedisConnectionError("down")

    monkeypatch.setattr(api, "get_redis", lambda: _Down())
    assert not await api._rate_limited("tenant-a", "203.0.113.1")

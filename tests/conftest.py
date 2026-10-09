"""Configuración común de los tests.

Los tests de esta suite son deliberadamente puros: no tocan la base de datos, ni
Redis, ni la red, ni requieren API key. Cubren la lógica de bootstrap que hacía
fallar la instalación desde cero.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# core.config exige varias variables; se fijan antes de importar nada del core.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "3sJ8kZ0mQ1nR5tV7wY9bC2eF4gH6jL8mN0pQ2rS4tU6=")
os.environ.setdefault("TOKEN_MASTER_KEYS", '{"k1": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}')
os.environ.setdefault("TOKEN_ACTIVE_KEY_ID", "k1")
os.environ.setdefault("APP_SECRET_KEY", "test-secret")


# --- Redis falso para `core.security.rate_limit` ---------------------------
# Sorted sets con TTL y reloj controlado; implementa solo lo que usa el
# limitador (pipeline con ZREMRANGEBYSCORE, ZADD, ZREMRANGEBYRANK, ZCARD,
# ZCOUNT, EXISTS y EXPIRE; SET con EX). Fixtures: `rl_clock` y `rl_redis`.

import pytest  # noqa: E402


class RateLimitClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeRateLimitRedis:
    """Sorted sets con TTL, leyendo el tiempo de `clock`."""

    def __init__(self, clock: RateLimitClock) -> None:
        self.clock = clock
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}

    def _expire_due(self) -> None:
        for key, at in list(self.expires_at.items()):
            if at <= self.clock():
                self.zsets.pop(key, None)
                self.strings.pop(key, None)
                self.expires_at.pop(key, None)

    def keys(self) -> set[str]:
        self._expire_due()
        return set(self.zsets) | set(self.strings)

    async def set(self, key: str, value: str, ex: int) -> bool:
        self._expire_due()
        self.strings[key] = value
        self.expires_at[key] = self.clock() + ex
        return True

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: FakeRateLimitRedis) -> None:
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

    def zcount(self, key: str, lo: str, hi: str) -> _FakePipeline:
        self.ops.append(("zcount", key, lo, hi))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self.ops.append(("expire", key, seconds))
        return self

    def exists(self, key: str) -> _FakePipeline:
        self.ops.append(("exists", key))
        return self

    async def execute(self) -> list:
        r = self.redis
        r._expire_due()
        results: list = []
        for op, key, *args in self.ops:
            if op == "exists":
                results.append(int(key in r.zsets or key in r.strings))
                continue
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
            elif op == "zcount":
                lo, hi = args
                assert hi == "+inf" and lo.startswith("("), "el doble solo soporta (x +inf"
                results.append(sum(1 for score in zset.values() if score > float(lo[1:])))
            elif op == "expire":
                if zset:  # como Redis: EXPIRE sobre una clave inexistente no hace nada
                    r.expires_at[key] = r.clock() + args[0]
                results.append(bool(zset))
            if not zset:
                r.zsets.pop(key, None)
                r.expires_at.pop(key, None)
        return results


@pytest.fixture
def rl_clock(monkeypatch: pytest.MonkeyPatch) -> RateLimitClock:
    from core.security import rate_limit

    clock = RateLimitClock()
    monkeypatch.setattr(rate_limit, "_clock", clock)
    return clock


@pytest.fixture
def rl_redis(monkeypatch: pytest.MonkeyPatch, rl_clock: RateLimitClock) -> FakeRateLimitRedis:
    from core.security import rate_limit

    fake = FakeRateLimitRedis(rl_clock)
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)
    return fake

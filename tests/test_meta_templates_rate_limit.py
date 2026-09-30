"""100 creaciones/hora por WABA; la 101 falla; una hora nueva resetea."""

from __future__ import annotations

import pytest

import core.gateway.meta_templates as templates_mod
from core.gateway.meta_templates import TemplateRateLimitError, _check_and_bump_create_rate_limit


class FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        pass


@pytest.mark.asyncio
async def test_100_creaciones_pasan_la_101_falla(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(templates_mod, "get_redis", lambda: fake)

    for _ in range(100):
        await _check_and_bump_create_rate_limit("waba-1")

    with pytest.raises(TemplateRateLimitError, match="100"):
        await _check_and_bump_create_rate_limit("waba-1")


@pytest.mark.asyncio
async def test_una_waba_no_afecta_a_otra(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(templates_mod, "get_redis", lambda: fake)

    for _ in range(100):
        await _check_and_bump_create_rate_limit("waba-a")

    # waba-b arranca en cero, no hereda el conteo de waba-a
    await _check_and_bump_create_rate_limit("waba-b")


@pytest.mark.asyncio
async def test_hora_distinta_resetea(monkeypatch):
    import core.gateway.meta_templates as mod

    fake = FakeRedis()
    monkeypatch.setattr(mod, "get_redis", lambda: fake)

    calls = iter(["2026-01-01-10"] * 100 + ["2026-01-01-11"])

    class FakeDatetime:
        @staticmethod
        def now(_tz):
            class _T:
                @staticmethod
                def strftime(_fmt):
                    return next(calls)

            return _T()

    monkeypatch.setattr(mod, "datetime", FakeDatetime)

    for _ in range(100):
        await _check_and_bump_create_rate_limit("waba-1")
    # nueva hora -> nueva key -> no debe fallar
    await _check_and_bump_create_rate_limit("waba-1")

"""Tests puros del tope diario y kill-switch del canal `waweb` (ADR-0074)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.waweb import limits
from core.waweb.policy import WaWebPolicy


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def incr(self, key: str) -> int:
        value = int(self.kv.get(key, "0")) + 1
        self.kv[key] = str(value)
        return value

    async def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl

    async def delete(self, key: str) -> None:
        self.kv.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(limits, "get_redis", lambda: fake)
    return fake


async def test_allow_send_bajo_el_tope(fake_redis: FakeRedis) -> None:
    policy = WaWebPolicy(daily_outbound_cap=2)
    assert await limits.allow_send(uuid.UUID(int=1), policy) is True


async def test_allow_send_cap_cero_bloquea(fake_redis: FakeRedis) -> None:
    assert await limits.allow_send(uuid.UUID(int=1), WaWebPolicy(daily_outbound_cap=0)) is False


async def test_allow_send_alcanza_el_tope(fake_redis: FakeRedis) -> None:
    tenant = uuid.UUID(int=1)
    policy = WaWebPolicy(daily_outbound_cap=2)
    await limits.record_outbound(tenant)
    await limits.record_outbound(tenant)
    assert await limits.outbound_count(tenant) == 2
    assert await limits.allow_send(tenant, policy) is False


async def test_record_outbound_pone_ttl_una_vez(fake_redis: FakeRedis) -> None:
    tenant = uuid.UUID(int=1)
    await limits.record_outbound(tenant)
    assert fake_redis.ttls[limits.cap_key(tenant)] == limits._CAP_TTL_S
    await limits.record_outbound(tenant)
    assert await limits.outbound_count(tenant) == 2


async def test_kill_switch_global_y_por_tenant(fake_redis: FakeRedis) -> None:
    tenant = uuid.UUID(int=1)
    policy = WaWebPolicy(daily_outbound_cap=10)
    fake_redis.kv[limits.tenant_pause_key(tenant)] = "true"
    assert await limits.is_paused(tenant) is True
    assert await limits.allow_send(tenant, policy) is False


async def test_desconexiones_se_acumulan(fake_redis: FakeRedis) -> None:
    tenant: Any = uuid.UUID(int=1)
    assert await limits.record_disconnect(tenant) == 1
    assert await limits.record_disconnect(tenant) == 2
    await limits.clear_disconnects(tenant)
    assert await limits.record_disconnect(tenant) == 1

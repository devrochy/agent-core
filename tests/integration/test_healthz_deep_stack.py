"""`/healthz/deep` contra el Postgres y el Redis reales del stack (ADR-0188)."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

import core.observability.salud as salud

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_db_y_redis_reales_responden_y_el_latido_cuenta(monkeypatch):
    monkeypatch.setattr(salud, "_cache", None)
    redis = salud._redis()
    previo = await redis.get(salud.LATIDO_KEY)
    try:
        await redis.set(salud.LATIDO_KEY, datetime.now(UTC).isoformat(), ex=60)
        assert await salud.medir_componentes() == {"db": "ok", "redis": "ok", "worker": "ok"}
        await redis.delete(salud.LATIDO_KEY)
        assert (await salud.medir_componentes())["worker"] == "fallo"
    finally:
        if previo:
            await redis.set(salud.LATIDO_KEY, previo, ex=180)

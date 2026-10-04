"""Tests puros del handoff de salida del canal `waweb` (ADR-0074).

Simula el connector con un Redis falso: no requiere red ni el socket real.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from agentcore_channel_waweb import adapters

from core.waweb import handoff


class FakeRedis:
    def __init__(self, reply: dict[str, Any] | None = None, timeout: bool = False) -> None:
        self.reply = reply
        self.timeout = timeout
        self.outbox: dict[str, list[str]] = {}
        self.kv: dict[str, str] = {}

    async def rpush(self, key: str, val: str) -> None:
        self.outbox.setdefault(key, []).append(val)

    async def blpop(self, key: str, timeout: int | None = None) -> tuple[str, str] | None:
        if self.timeout:
            return None
        return (key, json.dumps(self.reply or {}))

    async def set(self, key: str, val: str, ex: int | None = None) -> None:
        self.kv[key] = val

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.kv.pop(key, None) is not None else 0


@pytest.mark.asyncio
async def test_request_send_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(reply={"ok": True, "wamid": "WA-123"})
    fake.kv["waweb:ready:t1"] = "1"
    monkeypatch.setattr(handoff, "_redis", lambda: fake)

    wamid = await handoff.request_send(
        tenant_id="t1", connection_id="c1", to="573001112233", text="hola"
    )
    assert wamid == "WA-123"

    job = json.loads(fake.outbox["waweb:outbox:t1"][0])
    assert job["to"] == "573001112233"
    assert job["text"] == "hola"
    assert job["kind"] == "text"
    assert job["connection_id"] == "c1"


@pytest.mark.asyncio
async def test_request_send_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(timeout=True)
    fake.kv["waweb:ready:t1"] = "1"
    monkeypatch.setattr(handoff, "_redis", lambda: fake)
    with pytest.raises(handoff.WaWebError):
        await handoff.request_send(tenant_id="t1", connection_id=None, to="57300", text="x")


@pytest.mark.asyncio
async def test_request_send_sin_conexion_falla_rapido(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(reply={"ok": True, "wamid": "X"})
    monkeypatch.setattr(handoff, "_redis", lambda: fake)
    with pytest.raises(handoff.WaWebError, match="no está conectado"):
        await handoff.request_send(tenant_id="t1", connection_id=None, to="57300", text="x")


@pytest.mark.asyncio
async def test_reset_signal_se_publica_y_se_consume(monkeypatch: pytest.MonkeyPatch) -> None:
    """El reset del panel debe poder consumirse una sola vez (dispara QR nuevo)."""
    fake = FakeRedis()
    monkeypatch.setattr(handoff, "_redis", lambda: fake)

    assert await handoff.consume_reset("t1") is False  # nada pendiente
    await handoff.request_reset("t1")
    assert fake.kv["waweb:reset:t1"] == "1"
    assert await handoff.consume_reset("t1") is True   # se consume
    assert await handoff.consume_reset("t1") is False  # ya no está


@pytest.mark.asyncio
async def test_request_send_error_del_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(reply={"ok": False, "error": "sin sesión"})
    fake.kv["waweb:ready:t1"] = "1"
    monkeypatch.setattr(handoff, "_redis", lambda: fake)
    with pytest.raises(handoff.WaWebError, match="sin sesión"):
        await handoff.request_send(tenant_id="t1", connection_id=None, to="57300", text="x")


def test_qr_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(handoff, "_redis", lambda: fake)

    import asyncio

    asyncio.run(handoff.publish_qr("t1", "QR-DATA"))
    assert asyncio.run(handoff.read_qr("t1")) == "QR-DATA"
    assert asyncio.run(handoff.read_qr("otro")) is None
    asyncio.run(handoff.clear_qr("t1"))
    assert asyncio.run(handoff.read_qr("t1")) is None


@pytest.mark.asyncio
async def test_adapter_quita_prefijo_whatsapp(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(reply={"ok": True, "wamid": "WA-9"})
    tenant = str(uuid.UUID(int=7))
    fake.kv[f"waweb:ready:{tenant}"] = "1"
    monkeypatch.setattr(handoff, "_redis", lambda: fake)
    monkeypatch.setattr(adapters, "get_current_tenant_id", lambda: uuid.UUID(int=7))

    adapter = adapters.WaWebOutbound()
    wamid = await adapter.send_text({"connection_id": "c1"}, "whatsapp:+573001112233", "hola")
    assert wamid == "WA-9"

    job = json.loads(fake.outbox[f"waweb:outbox:{tenant}"][0])
    assert job["to"] == "573001112233"

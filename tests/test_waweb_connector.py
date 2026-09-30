"""Tests puros del runtime del connector `waweb` (ADR-0074): inbound y outbound.

Usan Redis falso y un backend falso: no requieren el socket ni la red.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from core.waweb import handoff, inbound, outbound
from core.waweb.policy import WaWebPolicy


class FakeRedis:
    def __init__(self, job: dict[str, Any] | None = None, popped: bool = True) -> None:
        self.job = job
        self.popped = popped
        self.xadds: list[dict[str, Any]] = []
        self.replies: dict[str, list[str]] = {}

    async def xadd(self, stream: str, fields: dict[str, Any]) -> str:
        self.xadds.append({"stream": stream, "fields": fields})
        return "1-1"

    async def blpop(self, key: str, timeout: int | None = None) -> tuple[str, str] | None:
        if self.job is None:
            return None
        return (key, json.dumps(self.job))

    async def rpush(self, key: str, val: str) -> None:
        self.replies.setdefault(key, []).append(val)


class FakeBackend:
    def __init__(self, wamid: str = "WA-1", error: str | None = None) -> None:
        self.wamid = wamid
        self.error = error
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, to: str, text: str, image_url: str | None = None) -> str:
        if self.error:
            raise RuntimeError(self.error)
        self.sent.append({"to": to, "text": text, "image_url": image_url})
        return self.wamid


@pytest.mark.asyncio
async def test_publish_inbound_arma_el_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(inbound, "get_redis", lambda: fake)

    async def _claim(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(inbound, "claim_message_once", _claim)

    ok = await inbound.publish_inbound(
        tenant_id=uuid.UUID(int=1),
        connection_external_id="waweb:1",
        sender="+573001112233",
        text="hola",
        wamid="WAMID-1",
    )
    assert ok is True
    entry = fake.xadds[0]
    assert entry["stream"] == "agent_core:inbound"
    f = entry["fields"]
    assert f["provider"] == "waweb"
    assert f["channel"] == "whatsapp"
    assert f["customer_external_id"] == "whatsapp:+573001112233"
    assert f["connection_external_id"] == "waweb:1"
    assert f["external_message_id"] == "WAMID-1"


@pytest.mark.asyncio
async def test_publish_inbound_duplicado_no_encola(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(inbound, "get_redis", lambda: fake)

    async def _claim(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(inbound, "claim_message_once", _claim)
    ok = await inbound.publish_inbound(
        tenant_id=uuid.UUID(int=1),
        connection_external_id="waweb:1",
        sender="57300",
        text="x",
        wamid="DUP",
    )
    assert ok is False
    assert fake.xadds == []


@pytest.mark.asyncio
async def test_process_job_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(handoff, "get_redis", lambda: fake)
    backend = FakeBackend(wamid="WA-77")
    delays: list[float] = []

    async def _sleep(s: float) -> None:
        delays.append(s)

    job = {"request_id": "r1", "connection_id": "c1", "to": "57300", "text": "hola", "image_url": ""}
    await outbound.process_job(job, backend, WaWebPolicy(send_min_delay_ms=0, send_max_delay_ms=0), sleep=_sleep)

    assert backend.sent[0]["to"] == "57300"
    reply = json.loads(fake.replies["waweb:reply:r1"][0])
    assert reply == {"ok": True, "wamid": "WA-77"}


@pytest.mark.asyncio
async def test_process_job_error_se_devuelve_al_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(handoff, "get_redis", lambda: fake)
    backend = FakeBackend(error="sin sesión")

    async def _sleep(s: float) -> None:
        return None

    job = {"request_id": "r2", "to": "57300", "text": "x"}
    await outbound.process_job(job, backend, WaWebPolicy(send_min_delay_ms=0, send_max_delay_ms=0), sleep=_sleep)

    reply = json.loads(fake.replies["waweb:reply:r2"][0])
    assert reply["ok"] is False
    assert "sin sesión" in reply["error"]


@pytest.mark.asyncio
async def test_process_job_aplica_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(handoff, "get_redis", lambda: fake)
    delays: list[float] = []

    async def _sleep(s: float) -> None:
        delays.append(s)

    job = {"request_id": "r3", "to": "57300", "text": "x"}
    await outbound.process_job(
        job, FakeBackend(), WaWebPolicy(send_min_delay_ms=1500, send_max_delay_ms=1500), sleep=_sleep
    )
    assert delays == [1.5]


@pytest.mark.asyncio
async def test_drain_once_sin_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff, "get_redis", lambda: FakeRedis(job=None))
    processed = await outbound.drain_once(
        uuid.UUID(int=1), FakeBackend(), WaWebPolicy(), sleep=_noop
    )
    assert processed is False


@pytest.mark.asyncio
async def test_drain_once_procesa_un_job(monkeypatch: pytest.MonkeyPatch) -> None:
    job = {"request_id": "r9", "to": "57300", "text": "hola"}
    fake = FakeRedis(job=job)
    monkeypatch.setattr(handoff, "get_redis", lambda: fake)
    backend = FakeBackend()
    processed = await outbound.drain_once(
        uuid.UUID(int=1), backend, WaWebPolicy(send_min_delay_ms=0, send_max_delay_ms=0), sleep=_noop
    )
    assert processed is True
    assert backend.sent
    assert "waweb:reply:r9" in fake.replies


class _SocketConFirmaReal:
    """Solo `send(*, to, text, image_url)`: el shape exacto de NeonizeSocket."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, to: str, text: str, image_url: str | None = None) -> str:
        self.sent.append({"to": to, "text": text, "image_url": image_url})
        return "WA-REAL"


@pytest.mark.asyncio
async def test_drain_once_usa_la_firma_real_del_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresión: el drenaje llamaba `backend.send_text`, un método que el
    socket real (NeonizeSocket) NO tiene. El envío moría con AttributeError en
    el conector, el reply viajaba al worker y el worker lo mandaba a la DLQ
    tras agotar reintentos (visto en vivo en el entorno test)."""
    job = {"request_id": "r10", "connection_id": "waweb:x", "to": "57300", "text": "hola"}
    fake = FakeRedis(job=job)
    monkeypatch.setattr(handoff, "get_redis", lambda: fake)
    backend = _SocketConFirmaReal()

    processed = await outbound.drain_once(
        uuid.UUID(int=1), backend, WaWebPolicy(send_min_delay_ms=0, send_max_delay_ms=0), sleep=_noop
    )

    assert processed is True
    assert backend.sent == [{"to": "57300", "text": "hola", "image_url": None}]
    assert json.loads(fake.replies["waweb:reply:r10"][0]) == {"ok": True, "wamid": "WA-REAL"}


def test_el_socket_real_y_el_drenaje_hablan_la_misma_firma() -> None:
    """Contrato: `NeonizeSocket.send` y el `SendBackend` del drenaje deben tener
    la misma firma. El módulo de Neonize se importa sin instalarlo: el puente
    se carga de forma perezosa dentro de `connect()`."""
    import inspect

    from core.waweb.backend_neonize import NeonizeSocket

    assert list(inspect.signature(NeonizeSocket.send).parameters) == [
        "self",
        "to",
        "text",
        "image_url",
    ]


async def _noop(_: float) -> None:
    return None

"""HU-SUB-03 / ADR-0107: outbox de eventos de dominio (parte pura)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.events.outbox import emit_domain, payload_para_handlers
from core.tasks.domain_events_dispatch import backoff_seconds


class FakeSession:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount
        self.executed = None

    async def execute(self, stmt):
        self.executed = stmt
        return SimpleNamespace(rowcount=self.rowcount)


@pytest.mark.asyncio
async def test_emit_domain_escribe_con_envoltura_del_tenant() -> None:
    tenant = uuid.uuid4()
    session = FakeSession(rowcount=1)

    insertado = await emit_domain(
        session,
        tenant_id=tenant,
        topic="payment.confirmed",
        dedupe_key="payment:ref-1:aprobado",
        payload={"reference": "OC-0001", "amount": 100.0},
    )

    assert insertado is True
    params = session.executed.compile().params
    assert params["tenant_id"] == tenant
    assert params["topic"] == "payment.confirmed"
    assert params["dedupe_key"] == "payment:ref-1:aprobado"
    assert params["status"] == "pendiente"
    payload = params["payload"]
    assert payload["reference"] == "OC-0001"
    assert payload["amount"] == 100.0
    assert payload["tenant_id"] == str(tenant)
    assert "occurred_at" in payload


@pytest.mark.asyncio
async def test_emit_domain_duplicado_no_inserta() -> None:
    session = FakeSession(rowcount=0)
    insertado = await emit_domain(
        session,
        tenant_id=uuid.uuid4(),
        topic="payment.confirmed",
        dedupe_key="dup",
        payload={},
    )
    assert insertado is False


def test_payload_para_handlers_quita_la_envoltura() -> None:
    payload = {"reference": "OC-0001", "amount": 1.0, "tenant_id": "x", "occurred_at": "y"}
    assert payload_para_handlers(payload) == {"reference": "OC-0001", "amount": 1.0}


def test_backoff_exponencial_acotado() -> None:
    assert backoff_seconds(0) == 30
    assert backoff_seconds(1) == 60
    assert backoff_seconds(4) == 600
    assert backoff_seconds(99) == 600


@pytest.mark.asyncio
async def test_emit_strict_levanta_si_un_handler_falla() -> None:
    from core.shared.events import EventBus

    bus = EventBus()
    vistos: list[str] = []

    async def _ok(**kwargs):
        vistos.append(kwargs["reference"])

    async def _falla(**kwargs):
        raise RuntimeError("handler roto")

    bus.subscribe("payment.confirmed", _ok)
    bus.subscribe("payment.confirmed", _falla)

    with pytest.raises(RuntimeError, match="handler roto"):
        await bus.emit_strict("payment.confirmed", reference="OC-0001")
    # El handler sano corrió igual antes de levantar.
    assert vistos == ["OC-0001"]

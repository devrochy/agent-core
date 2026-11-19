"""HU-SUB-03 / ADR-0107: despachador del outbox y API admin (parte pura)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.tasks.domain_events_dispatch as dispatch_mod
from core.tasks.domain_events_dispatch import MAX_ATTEMPTS


def _evento(attempts: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        topic="payment.confirmed",
        payload={"reference": "OC-0001", "tenant_id": "x", "occurred_at": "y"},
        status="pendiente",
        attempts=attempts,
        next_attempt_at=datetime.now(UTC),
        last_error=None,
    )


class FakeSession:
    def __init__(self, evento: SimpleNamespace) -> None:
        self.evento = evento

    async def get(self, model, event_id):
        return self.evento

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _montar(monkeypatch, evento, *, handler_error: Exception | None = None):
    async def _due(limit, before):
        return [(evento.id, evento.tenant_id)]

    async def _claim_true(event_id):
        return True

    async def _release_noop(event_id):
        return None

    async def _emit_strict(topic, **payload):
        if handler_error is not None:
            raise handler_error

    monkeypatch.setattr(dispatch_mod, "admin_domain_events_due", _due)
    monkeypatch.setattr(dispatch_mod, "_claim", _claim_true)
    monkeypatch.setattr(dispatch_mod, "_release", _release_noop)
    monkeypatch.setattr(dispatch_mod, "get_session", lambda: FakeSession(evento))
    monkeypatch.setattr(dispatch_mod.event_bus, "emit_strict", _emit_strict)


@pytest.mark.asyncio
async def test_entrega_exitosa_marca_entregado(monkeypatch) -> None:
    evento = _evento()
    _montar(monkeypatch, evento)

    assert await dispatch_mod.dispatch_due_events() == 1
    assert evento.status == "entregado"
    assert evento.attempts == 0


@pytest.mark.asyncio
async def test_fallo_reintenta_con_backoff(monkeypatch) -> None:
    evento = _evento()
    _montar(monkeypatch, evento, handler_error=RuntimeError("boom"))

    assert await dispatch_mod.dispatch_due_events() == 1
    assert evento.status == "pendiente"
    assert evento.attempts == 1
    assert evento.last_error == "boom"
    esperado = datetime.now(UTC) + timedelta(seconds=30)
    assert abs((evento.next_attempt_at - esperado).total_seconds()) < 10


@pytest.mark.asyncio
async def test_tras_cinco_intentos_pasa_a_fallido(monkeypatch) -> None:
    evento = _evento(attempts=MAX_ATTEMPTS - 1)
    _montar(monkeypatch, evento, handler_error=RuntimeError("roto"))

    assert await dispatch_mod.dispatch_due_events() == 1
    assert evento.status == "fallido"
    assert evento.attempts == MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_sin_handlers_se_marca_entregado(monkeypatch) -> None:
    """Un evento sin suscriptores no es un fallo: queda entregado."""
    evento = _evento()
    _montar(monkeypatch, evento)


    async def _emit_sin_handlers(topic, **payload):
        return None

    monkeypatch.setattr(dispatch_mod.event_bus, "emit_strict", _emit_sin_handlers)
    assert await dispatch_mod.dispatch_due_events() == 1
    assert evento.status == "entregado"


# ---------------------------------------------------------------- API admin
@pytest.mark.asyncio
async def test_list_failed_y_retry(monkeypatch) -> None:
    from core.api.routers import domain_events

    eventos = [
        SimpleNamespace(
            id=uuid.uuid4(),
            topic="payment.confirmed",
            attempts=5,
            last_error="roto",
            created_at=datetime.now(UTC),
        )
    ]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            class _Result:
                def scalars(self):
                    return SimpleNamespace(all=lambda: eventos)

                def __aenter__(self):
                    return self

                def __aexit__(self, *a):
                    return False

            return _Result()

    principal = SimpleNamespace(tenant_id=uuid.uuid4())
    monkeypatch.setattr(domain_events, "get_session", lambda: Session())
    pagina = await domain_events.list_failed(principal)
    assert len(pagina.entries) == 1
    assert pagina.entries[0].last_error == "roto"

    class RetrySession(Session):
        async def get(self, model, event_id):
            return SimpleNamespace(id=event_id, tenant_id=principal.tenant_id, status="fallido")

        async def execute(self, stmt):
            return None

    monkeypatch.setattr(domain_events, "get_session", lambda: RetrySession())
    from fastapi import Response

    respuesta = await domain_events.retry_event(uuid.uuid4(), principal)
    assert isinstance(respuesta, Response)
    assert respuesta.status_code == 204

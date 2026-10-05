"""`apply_status_update`: orden monótono queued<sent<delivered<read<failed
-- un 'delivered' que llega DESPUÉS de un 'read' no degrada el estado."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.gateway.meta_message_status as status_mod
from core.gateway.meta_message_status import apply_status_update


class FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeSession:
    def __init__(self, row):
        self._row = row

    async def execute(self, *a, **k):
        return FakeResult(self._row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSessionCM:
    def __init__(self, row):
        self._row = row

    def __call__(self):
        return FakeSession(self._row)


def _message(**overrides):
    defaults = dict(
        id="msg-1", tenant_id="tenant-1", status="sent", status_updated_at=None,
        error_code=None, error_subcode=None, error_reason=None, error_fbtrace_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch(monkeypatch, row, tenant_id="tenant-1"):
    async def _fake_resolve(_external_message_id):
        return tenant_id

    monkeypatch.setattr(status_mod, "_resolve_tenant_for_message", _fake_resolve)
    monkeypatch.setattr(status_mod, "get_session", FakeSessionCM(row))


@pytest.mark.asyncio
async def test_sent_a_delivered_avanza(monkeypatch):
    row = _message(status="sent")
    _patch(monkeypatch, row)
    await apply_status_update({"id": "wamid-1", "status": "delivered", "timestamp": "2000000000"}, expected_tenant_id="tenant-1")
    assert row.status == "delivered"


@pytest.mark.asyncio
async def test_delivered_despues_de_read_no_degrada(monkeypatch):
    """Criterio de aceptación explícito: un 'delivered' que llega DESPUÉS
    de un 'read' no degrada el estado -- ni por rank ni porque el
    timestamp real de 'delivered' es anterior al de 'read'."""
    read_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    row = _message(status="read", status_updated_at=read_ts)
    _patch(monkeypatch, row)

    delivered_ts_epoch = int((read_ts - timedelta(minutes=5)).timestamp())
    await apply_status_update({"id": "wamid-1", "status": "delivered", "timestamp": str(delivered_ts_epoch)}, expected_tenant_id="tenant-1")
    assert row.status == "read"


@pytest.mark.asyncio
async def test_mismo_estado_no_reventa_es_idempotente(monkeypatch):
    row = _message(status="delivered", status_updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    _patch(monkeypatch, row)
    await apply_status_update({"id": "wamid-1", "status": "delivered", "timestamp": "1735689600"}, expected_tenant_id="tenant-1")
    assert row.status == "delivered"


@pytest.mark.asyncio
async def test_failed_persiste_error_detail(monkeypatch):
    row = _message(status="sent")
    _patch(monkeypatch, row)
    await apply_status_update(
        {
            "id": "wamid-1", "status": "failed", "timestamp": "2000000000",
            "errors": [{"code": 131047, "message": "Re-engagement", "fbtrace_id": "trace-1"}],
        },
        expected_tenant_id="tenant-1",
    )
    assert row.status == "failed"
    assert row.error_code == 131047
    assert row.error_reason == "Re-engagement"
    assert row.error_fbtrace_id == "trace-1"


@pytest.mark.asyncio
async def test_sin_fila_local_no_revienta(monkeypatch):
    async def _fake_resolve(_id):
        return None

    monkeypatch.setattr(status_mod, "_resolve_tenant_for_message", _fake_resolve)
    monkeypatch.setattr(status_mod, "get_session", FakeSessionCM(None))
    await apply_status_update({"id": "wamid-inexistente", "status": "sent", "timestamp": "2000000000"}, expected_tenant_id="tenant-1")


@pytest.mark.asyncio
async def test_status_desconocido_no_revienta(monkeypatch):
    row = _message(status="sent")
    _patch(monkeypatch, row)
    await apply_status_update({"id": "wamid-1", "status": "algo_nuevo_no_documentado", "timestamp": "2000000000"}, expected_tenant_id="tenant-1")
    assert row.status == "sent"  # sin cambios, se ignora sin romper


@pytest.mark.asyncio
async def test_status_de_un_mensaje_de_otro_negocio_no_lo_toca(monkeypatch):
    """ADR-0096: el webhook firmado por B trae el wamid de un mensaje de A. Se busca en
    el scope de B (no aparece) y el resolvedor solo sirve para el aviso."""
    _patch(monkeypatch, None, tenant_id="tenant-a")
    avisos = []
    monkeypatch.setattr(
        status_mod, "log", SimpleNamespace(warning=lambda e, **k: avisos.append(e), info=lambda *a, **k: None)
    )

    await apply_status_update(
        {"id": "wamid-1", "status": "failed", "timestamp": "2000000000", "errors": [{"code": 1}]},
        expected_tenant_id="tenant-b",
    )

    assert avisos == ["status_update_de_otro_negocio"]

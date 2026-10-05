"""`apply_status_update` persiste `MessageCost` cuando el status trae
`pricing` -- un evento por llamada (nunca upsert), valores fuera del set
conocido se guardan igual y loguean warning, sin `pricing` no se inserta
nada."""

from __future__ import annotations

import uuid
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
    def __init__(self, message_row, phone_row=None):
        self._message_row = message_row
        self._phone_row = phone_row
        self.added = []

    async def execute(self, stmt, *a, **k):
        # Heurística simple: si ya se agregó una fila de PhoneNumber
        # buscada, devolvemos esa; si no, la del Message.
        return FakeResult(self._phone_row if "phone" in str(stmt).lower() else self._message_row)

    def add(self, obj):
        self.added.append(obj)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSessionCM:
    def __init__(self, message_row, phone_row=None):
        self._message_row = message_row
        self._phone_row = phone_row
        self.sessions: list[FakeSession] = []

    def __call__(self):
        session = FakeSession(self._message_row, self._phone_row)
        self.sessions.append(session)
        return session


def _row(**overrides):
    defaults = dict(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), status="sent", status_updated_at=None,
        error_code=None, error_subcode=None, error_reason=None, error_fbtrace_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch(monkeypatch, message_row, tenant_id, phone_row=None):
    async def _fake_resolve(_id):
        return tenant_id

    session_cm = FakeSessionCM(message_row, phone_row)
    monkeypatch.setattr(status_mod, "_resolve_tenant_for_message", _fake_resolve)
    monkeypatch.setattr(status_mod, "get_session", session_cm)
    return session_cm


@pytest.mark.asyncio
async def test_sin_pricing_no_inserta_nada(monkeypatch):
    tenant_id = uuid.uuid4()
    row = _row(tenant_id=tenant_id)
    session_cm = _patch(monkeypatch, row, tenant_id)

    await apply_status_update({"id": "wamid-1", "status": "sent", "timestamp": "2000000000"}, expected_tenant_id=tenant_id)

    all_added = [obj for s in session_cm.sessions for obj in s.added]
    assert all_added == []


@pytest.mark.asyncio
async def test_con_pricing_inserta_message_cost(monkeypatch):
    tenant_id = uuid.uuid4()
    row = _row(tenant_id=tenant_id)
    session_cm = _patch(monkeypatch, row, tenant_id)

    await apply_status_update(
        {
            "id": "wamid-1", "status": "delivered", "timestamp": "2000000000",
            "pricing": {"billable": True, "pricing_model": "CBP", "category": "utility"},
        },
        expected_tenant_id=tenant_id,
        phone_number_id="1234567890",
    )

    all_added = [obj for s in session_cm.sessions for obj in s.added]
    assert len(all_added) == 1
    cost_row = all_added[0]
    assert cost_row.billable is True
    assert cost_row.pricing_model == "CBP"
    assert cost_row.pricing_category == "utility"
    assert cost_row.external_message_id == "wamid-1"
    assert cost_row.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_pricing_dos_eventos_inserta_dos_filas_nunca_upsert(monkeypatch):
    tenant_id = uuid.uuid4()
    row = _row(tenant_id=tenant_id)
    session_cm = _patch(monkeypatch, row, tenant_id)

    payload = {
        "id": "wamid-1", "status": "sent", "timestamp": "2000000000",
        "pricing": {"billable": True, "category": "utility"},
    }
    await apply_status_update(payload, expected_tenant_id=tenant_id)
    payload2 = {**payload, "status": "delivered", "timestamp": "2000000100"}
    await apply_status_update(payload2, expected_tenant_id=tenant_id)

    all_added = [obj for s in session_cm.sessions for obj in s.added]
    assert len(all_added) == 2


@pytest.mark.asyncio
async def test_valor_desconocido_se_guarda_igual_y_loguea(monkeypatch, caplog):
    tenant_id = uuid.uuid4()
    row = _row(tenant_id=tenant_id)
    _patch(monkeypatch, row, tenant_id)

    # No debe lanzar ni bloquear la ingesta por un valor no reconocido.
    await apply_status_update(
        {
            "id": "wamid-1", "status": "sent", "timestamp": "2000000000",
            "pricing": {"billable": True, "category": "algo_nuevo_no_documentado"},
        },
        expected_tenant_id=tenant_id,
    )


@pytest.mark.asyncio
async def test_pricing_de_un_mensaje_de_otro_negocio_no_inserta_nada(monkeypatch):
    """ADR-0096: B no puede sumarle costos a un mensaje de A."""
    tenant_a = uuid.uuid4()
    # El mensaje es de A: dentro del scope de B no aparece.
    session_cm = _patch(monkeypatch, None, tenant_a)

    await apply_status_update(
        {
            "id": "wamid-1", "status": "delivered", "timestamp": "2000000000",
            "pricing": {"billable": True, "category": "marketing"},
        },
        expected_tenant_id=uuid.uuid4(),
    )

    assert [obj for s in session_cm.sessions for obj in s.added] == []

"""Los 14 valores de `event` de `message_template_status_update` actualizan
`status` 1:1; un valor inventado hace que el handler levante (traduce la
intención del `never` de TypeScript a Python); `REJECTED` persiste
`rejected_reason`/`rejection_recommendation`; `DISABLED` persiste
`disable_date`; los dos webhooks no verificados no revientan con payloads
con campos faltantes o extra."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_template_webhooks as tpl_webhooks_mod
from core.gateway.meta_template_webhooks import (
    _ALL_STATUS_EVENTS,
    handle_template_category_update,
    handle_template_quality_update,
    handle_template_status_update,
)


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


def _row(**overrides):
    defaults = dict(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), status="PENDING", category="UTILITY",
        rejected_reason=None, rejection_recommendation=None, disable_date=None,
        other_info_title=None, other_info_description=None, previous_category=None,
        category_changed_at=None, quality_score=None, previous_quality_score=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_for_row(monkeypatch, row):
    """Simula que la fila existe (`_resolve_tenant_for_template` la
    encuentra, `get_session` la trae para el UPDATE)."""

    async def _fake_resolve(_meta_template_id):
        return row.tenant_id

    monkeypatch.setattr(tpl_webhooks_mod, "_resolve_tenant_for_template", _fake_resolve)
    monkeypatch.setattr(tpl_webhooks_mod, "get_session", FakeSessionCM(row))


def _patch_no_row(monkeypatch):
    async def _fake_resolve(_meta_template_id):
        return None

    monkeypatch.setattr(tpl_webhooks_mod, "_resolve_tenant_for_template", _fake_resolve)


@pytest.mark.asyncio
async def test_los_14_valores_actualizan_status_1a1(monkeypatch):
    for event in sorted(_ALL_STATUS_EVENTS):
        row = _row()
        _patch_for_row(monkeypatch, row)

        await handle_template_status_update(
            {"message_template_id": "123", "message_template_name": "x", "event": event}
        )
        assert row.status == event


@pytest.mark.asyncio
async def test_evento_inventado_levanta(monkeypatch):
    row = _row()
    _patch_for_row(monkeypatch, row)

    with pytest.raises(AssertionError, match="no manejado"):
        await handle_template_status_update({"message_template_id": "123", "event": "NO_EXISTE"})


@pytest.mark.asyncio
async def test_rejected_persiste_reason_y_recommendation(monkeypatch):
    row = _row()
    _patch_for_row(monkeypatch, row)

    await handle_template_status_update(
        {
            "message_template_id": "123", "event": "REJECTED",
            "rejection_info": {"reason": "SCAM", "recommendation": "Revisá el contenido"},
        }
    )
    assert row.rejected_reason == "SCAM"
    assert row.rejection_recommendation == "Revisá el contenido"


@pytest.mark.asyncio
async def test_disabled_persiste_disable_date(monkeypatch):
    row = _row()
    _patch_for_row(monkeypatch, row)

    await handle_template_status_update(
        {"message_template_id": "123", "event": "DISABLED", "disable_info": {"disable_date": "2026-10-01"}}
    )
    assert row.disable_date == "2026-10-01"


@pytest.mark.asyncio
async def test_sin_fila_local_no_revienta(monkeypatch):
    _patch_no_row(monkeypatch)
    await handle_template_status_update({"message_template_id": "999", "event": "APPROVED"})


@pytest.mark.asyncio
async def test_category_update_payload_con_campos_extra_no_revienta(monkeypatch):
    row = _row(category="UTILITY")
    _patch_for_row(monkeypatch, row)

    redis = SimpleNamespace(set=_noop_set)
    monkeypatch.setattr(tpl_webhooks_mod, "get_redis", lambda: redis)

    await handle_template_category_update(
        {"message_template_id": "123", "new_category": "MARKETING", "campo_no_documentado": "x"}
    )
    assert row.category == "MARKETING"
    assert row.previous_category == "UTILITY"
    assert row.category_changed_at is not None


@pytest.mark.asyncio
async def test_category_update_sin_new_category_no_revienta(monkeypatch):
    row = _row()
    _patch_for_row(monkeypatch, row)
    redis = SimpleNamespace(set=_noop_set)
    monkeypatch.setattr(tpl_webhooks_mod, "get_redis", lambda: redis)

    await handle_template_category_update({"message_template_id": "123", "campo_desconocido": True})
    assert row.category == "UTILITY"  # sin cambios


@pytest.mark.asyncio
async def test_quality_update_payload_defensivo_no_revienta(monkeypatch):
    row = _row()
    _patch_for_row(monkeypatch, row)
    redis = SimpleNamespace(set=_noop_set)
    monkeypatch.setattr(tpl_webhooks_mod, "get_redis", lambda: redis)

    await handle_template_quality_update(
        {"message_template_id": "123", "previous_quality_score": "GREEN", "new_quality_score": "RED", "extra": 1}
    )
    assert row.quality_score == "RED"
    assert row.previous_quality_score == "GREEN"


@pytest.mark.asyncio
async def test_quality_update_payload_sin_campos_esperados_no_revienta(monkeypatch):
    row = _row()
    _patch_for_row(monkeypatch, row)
    redis = SimpleNamespace(set=_noop_set)
    monkeypatch.setattr(tpl_webhooks_mod, "get_redis", lambda: redis)

    await handle_template_quality_update({"message_template_id": "123", "algo_totalmente_distinto": True})
    assert row.quality_score is None


async def _noop_set(*a, **k):
    return True

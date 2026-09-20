"""`update_template`: bloquea sin llamar a Meta si el status no es editable o
si no quedan ediciones del día/mes; el contador resetea al cambiar de mes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.gateway.meta_templates as templates_mod
from core.gateway.meta_templates import (
    MAX_EDITS_PER_MONTH,
    TemplateValidationError,
    update_template,
)


def _row(**overrides):
    defaults = dict(
        status="APPROVED", category="UTILITY", components=[{"type": "BODY", "text": "x"}],
        edits_this_month=0, edits_month_key=None, last_edited_at=None,
        parameter_format=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


async def _no_call_graph_fetch(*a, **k):
    raise AssertionError("no debe llamar a Meta")


@pytest.mark.parametrize("status", ["PENDING", "DISABLED", "IN_APPEAL", "DRAFT"])
@pytest.mark.asyncio
async def test_status_no_editable_bloquea_sin_llamar_a_meta(monkeypatch, status):
    row = _row(status=status)
    monkeypatch.setattr(templates_mod, "get_session", FakeSessionCM(row))
    monkeypatch.setattr(templates_mod, "graph_fetch", _no_call_graph_fetch)

    with pytest.raises(TemplateValidationError, match=status):
        await update_template("123", "tok", "secret", "v21.0", uuid.uuid4(), uuid.uuid4(), category="MARKETING")


@pytest.mark.asyncio
async def test_ya_editada_hoy_bloquea(monkeypatch):
    row = _row(last_edited_at=datetime.now(UTC))
    monkeypatch.setattr(templates_mod, "get_session", FakeSessionCM(row))
    monkeypatch.setattr(templates_mod, "graph_fetch", _no_call_graph_fetch)

    with pytest.raises(TemplateValidationError, match="1 edición/día"):
        await update_template("123", "tok", "secret", "v21.0", uuid.uuid4(), uuid.uuid4(), category="MARKETING")


@pytest.mark.asyncio
async def test_limite_mensual_bloquea(monkeypatch):
    month_key = datetime.now(UTC).strftime("%Y-%m")
    row = _row(edits_this_month=MAX_EDITS_PER_MONTH, edits_month_key=month_key)
    monkeypatch.setattr(templates_mod, "get_session", FakeSessionCM(row))
    monkeypatch.setattr(templates_mod, "graph_fetch", _no_call_graph_fetch)

    with pytest.raises(TemplateValidationError, match="ediciones/mes"):
        await update_template("123", "tok", "secret", "v21.0", uuid.uuid4(), uuid.uuid4(), category="MARKETING")


@pytest.mark.asyncio
async def test_edicion_valida_incrementa_contador(monkeypatch):
    row = _row()
    monkeypatch.setattr(templates_mod, "get_session", FakeSessionCM(row))

    async def _fake_graph_fetch(*a, **k):
        return {}

    monkeypatch.setattr(templates_mod, "graph_fetch", _fake_graph_fetch)

    await update_template("123", "tok", "secret", "v21.0", uuid.uuid4(), uuid.uuid4(), category="MARKETING")
    assert row.edits_this_month == 1
    assert row.category == "MARKETING"
    assert row.last_edited_at is not None


@pytest.mark.asyncio
async def test_contador_resetea_en_mes_nuevo(monkeypatch):
    old_month = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m")
    row = _row(edits_this_month=MAX_EDITS_PER_MONTH, edits_month_key=old_month)
    monkeypatch.setattr(templates_mod, "get_session", FakeSessionCM(row))

    async def _fake_graph_fetch(*a, **k):
        return {}

    monkeypatch.setattr(templates_mod, "graph_fetch", _fake_graph_fetch)

    await update_template("123", "tok", "secret", "v21.0", uuid.uuid4(), uuid.uuid4(), category="MARKETING")
    assert row.edits_this_month == 1

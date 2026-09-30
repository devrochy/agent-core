"""Cada check parsea defensivamente (`extra="allow"`), `expires_at==0` ->
"no expira", `is_valid=false` -> `ok=False` + `repair_action` correcto."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import core.gateway.meta_health as health_mod
from core.gateway.meta_health import (
    check_billing,
    check_number_registered,
    check_token_valid,
    check_webhook_override,
    check_webhook_subscribed,
    decide_status,
)


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = ""
        self.headers = httpx.Headers({})

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_expires_at_cero_significa_no_expira(monkeypatch):
    response = _FakeResponse({"data": {"is_valid": True, "expires_at": 0}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is True


@pytest.mark.asyncio
async def test_is_valid_false_marca_ok_false_con_repair_reauthorize(monkeypatch):
    response = _FakeResponse({"data": {"is_valid": False}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    async def _noop_alert(*a, **k):
        return None

    monkeypatch.setattr("core.shared.alerts.send_alert", _noop_alert)

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is False
    assert check.repair_action == "reauthorize"


@pytest.mark.asyncio
async def test_token_por_vencer_en_menos_de_7_dias_falla(monkeypatch):
    soon = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    response = _FakeResponse({"data": {"is_valid": True, "expires_at": soon}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    async def _noop_alert(*a, **k):
        return None

    monkeypatch.setattr("core.shared.alerts.send_alert", _noop_alert)

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is False
    assert check.repair_action == "refresh_token"


@pytest.mark.asyncio
async def test_campos_desconocidos_de_debug_token_no_revientan(monkeypatch):
    response = _FakeResponse({"data": {"is_valid": True, "expires_at": 0, "campo_nuevo_no_documentado": "x"}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is True


@pytest.mark.asyncio
async def test_webhook_subscribed_data_vacio_falla_con_repair(monkeypatch):
    async def _fake_graph_fetch(*a, **k):
        return {"data": []}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_webhook_subscribed("waba-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is False
    assert check.repair_action == "subscribe_waba"


@pytest.mark.asyncio
async def test_number_registered_no_verificado_falla(monkeypatch):
    async def _fake_graph_fetch(*a, **k):
        return {"quality_rating": "GREEN", "code_verification_status": "PENDING"}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_number_registered("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is False


@pytest.mark.asyncio
async def test_number_registered_quality_red_dispara_alerta(monkeypatch):
    alerts = []

    async def _fake_graph_fetch(*a, **k):
        return {"quality_rating": "RED", "code_verification_status": "VERIFIED"}

    async def _fake_alert(title, detail, severity="warning"):
        alerts.append((title, severity))

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr("core.shared.alerts.send_alert", _fake_alert)

    check = await check_number_registered("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is True
    assert alerts == [("Calidad de número en RED", "critical")]


@pytest.mark.asyncio
async def test_webhook_override_sin_override_configurado_pasa(monkeypatch):
    async def _fake_graph_fetch(*a, **k):
        return {"webhook_configuration": {}}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_webhook_override("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is True


def test_billing_siempre_ok_none():
    from types import SimpleNamespace

    connection = SimpleNamespace(health={})
    check = check_billing(connection)
    assert check.ok is None
    assert check.repair_action == "configure_billing"


def test_decide_status_ok_todos_pasan():
    checks = {"token_valid": {"ok": True}, "templates": {"ok": True}}
    status, reason = decide_status(checks)
    assert status == "connected"
    assert reason is None


def test_decide_status_falla_no_critica_da_degraded():
    checks = {"token_valid": {"ok": True}, "templates": {"ok": False}}
    status, _reason = decide_status(checks)
    assert status == "degraded"


def test_decide_status_falla_critica_da_error():
    checks = {"token_valid": {"ok": False}, "templates": {"ok": True}}
    status, _reason = decide_status(checks)
    assert status == "error"

"""`scripts/verify_production.py`: exit 1 si algún check da `False`, exit 0
si todos pasan/son `None`."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_production.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_production_script", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_production_script"] = module
    spec.loader.exec_module(module)
    return module


def _fake_check(ok):
    from types import SimpleNamespace

    return SimpleNamespace(ok=ok, key="fake_check", detail="detalle")


@pytest.mark.asyncio
async def test_exit_1_si_algun_check_falla(monkeypatch):
    script = _load_script()
    import core.gateway.meta_production_readiness as readiness_mod

    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _async_returning(_fake_check(False)))
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_prod_secret_differs_from_dev", lambda *a, **k: _fake_check(None))
    monkeypatch.setattr(readiness_mod, "check_token_keys_configured", lambda: _fake_check(True))
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_rls_tests_pass", lambda: _fake_check(True))
    monkeypatch.setattr(readiness_mod, "check_no_secret_leak_in_templates", lambda: _fake_check(True))

    exit_code = await script._run(tenant_id=None)
    assert exit_code == 1


@pytest.mark.asyncio
async def test_exit_0_si_todos_pasan_o_son_none(monkeypatch):
    script = _load_script()
    import core.gateway.meta_production_readiness as readiness_mod

    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_prod_secret_differs_from_dev", lambda *a, **k: _fake_check(None))
    monkeypatch.setattr(readiness_mod, "check_token_keys_configured", lambda: _fake_check(True))
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_rls_tests_pass", lambda: _fake_check(None))
    monkeypatch.setattr(readiness_mod, "check_no_secret_leak_in_templates", lambda: _fake_check(True))

    exit_code = await script._run(tenant_id=None)
    assert exit_code == 0


@pytest.mark.asyncio
async def test_tenant_id_incluye_el_check_de_salud_de_conexiones(monkeypatch):
    script = _load_script()
    import core.gateway.meta_production_readiness as readiness_mod

    calls = []

    async def _fake_connections_health(tid):
        calls.append(tid)
        return _fake_check(True)

    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_prod_secret_differs_from_dev", lambda *a, **k: _fake_check(None))
    monkeypatch.setattr(readiness_mod, "check_token_keys_configured", lambda: _fake_check(True))
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _async_returning(_fake_check(True)))
    monkeypatch.setattr(readiness_mod, "check_rls_tests_pass", lambda: _fake_check(True))
    monkeypatch.setattr(readiness_mod, "check_no_secret_leak_in_templates", lambda: _fake_check(True))
    monkeypatch.setattr(readiness_mod, "check_connections_health_except_billing", _fake_connections_health)

    tid = uuid.uuid4()
    exit_code = await script._run(tenant_id=tid)
    assert exit_code == 0
    assert calls == [tid]


def _async_returning(value):
    async def _fn(*a, **k):
        return value

    return _fn

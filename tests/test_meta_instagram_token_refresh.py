"""El cron de refresco de tokens de Instagram Login (ruta b) nunca refresca
un token con menos de 24h de antigüedad, aunque esté por vencer, y marca la
cuenta `degraded` sin reintentar en el mismo tick si Meta rechaza el
refresco."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.tasks.instagram_token_refresh as refresh_mod
from core.gateway.meta_errors import GraphError
from core.gateway.meta_instagram import LONG_LIVED_DAYS


class FakeSession:
    def __init__(self, account):
        self._account = account

    async def execute(self, *a, **k):
        class _Result:
            def __init__(self, row):
                self._row = row

            def scalars(self):
                return self

            def all(self):
                return [self._row] if self._row else []

            def scalar_one_or_none(self):
                return self._row

        return _Result(self._account)

    async def get(self, model, id_):
        return None


class FakeSessionCM:
    def __init__(self, account):
        self._account = account

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeSession(self._account)

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_token_con_menos_de_24h_no_se_refresca(monkeypatch):
    connection_id, ig_user_id, tenant_id = uuid.uuid4(), "ig-user-1", uuid.uuid4()
    # Emitido hace 2 horas -- expires_at = ahora + 60d - (60d - 2h) = ahora + 2h...
    # más simple: issued_at = ahora - 2h -> expires_at = issued_at + 60d
    issued_at = datetime.now(UTC) - timedelta(hours=2)
    expires_at = issued_at + timedelta(days=LONG_LIVED_DAYS)

    async def _fake_get_expiry(*a, **k):
        return expires_at

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe intentar refrescar un token con menos de 24h")

    monkeypatch.setattr(refresh_mod.meta_token_store, "get_token_expiry", _fake_get_expiry)
    monkeypatch.setattr(refresh_mod.meta_token_store, "get_token", _debe_no_llamarse)
    monkeypatch.setattr(refresh_mod, "_refresh_one", _debe_no_llamarse)

    await refresh_mod._maybe_refresh_one(connection_id, ig_user_id, tenant_id)


@pytest.mark.asyncio
async def test_token_elegible_se_refresca(monkeypatch):
    connection_id, ig_user_id, tenant_id = uuid.uuid4(), "ig-user-2", uuid.uuid4()
    # Emitido hace 55 días -- edad > 24h, y expires_at (60d - 55d = 5d) < ventana de 10d.
    issued_at = datetime.now(UTC) - timedelta(days=55)
    expires_at = issued_at + timedelta(days=LONG_LIVED_DAYS)

    async def _fake_get_expiry(*a, **k):
        return expires_at

    async def _fake_get_token(*a, **k):
        return "tok-viejo"

    refreshed = {}

    async def _fake_refresh_one(token):
        refreshed["called_with"] = token
        return "tok-nuevo", datetime.now(UTC) + timedelta(days=LONG_LIVED_DAYS)

    put_calls = []

    async def _fake_put_token(connection_id, kind, subject_id, token, **kwargs):
        put_calls.append((kind, subject_id, token))

    monkeypatch.setattr(refresh_mod.meta_token_store, "get_token_expiry", _fake_get_expiry)
    monkeypatch.setattr(refresh_mod.meta_token_store, "get_token", _fake_get_token)
    monkeypatch.setattr(refresh_mod.meta_token_store, "put_token", _fake_put_token)
    monkeypatch.setattr(refresh_mod, "_refresh_one", _fake_refresh_one)
    monkeypatch.setattr(refresh_mod, "get_session", FakeSessionCM(None))

    await refresh_mod._maybe_refresh_one(connection_id, ig_user_id, tenant_id)

    assert refreshed["called_with"] == "tok-viejo"
    assert put_calls == [("long_lived_user", ig_user_id, "tok-nuevo")]


@pytest.mark.asyncio
async def test_refresco_rechazado_marca_degraded_sin_reintentar(monkeypatch):
    connection_id, ig_user_id, tenant_id = uuid.uuid4(), "ig-user-3", uuid.uuid4()
    issued_at = datetime.now(UTC) - timedelta(days=55)
    expires_at = issued_at + timedelta(days=LONG_LIVED_DAYS)

    async def _fake_get_expiry(*a, **k):
        return expires_at

    async def _fake_get_token(*a, **k):
        return "tok-revocado"

    calls = {"n": 0}

    async def _fake_refresh_one(token):
        calls["n"] += 1
        raise GraphError("El token fue revocado", category="AUTH", code=190)

    account = SimpleNamespace(status="connected", status_reason=None)
    monkeypatch.setattr(refresh_mod.meta_token_store, "get_token_expiry", _fake_get_expiry)
    monkeypatch.setattr(refresh_mod.meta_token_store, "get_token", _fake_get_token)
    monkeypatch.setattr(refresh_mod, "_refresh_one", _fake_refresh_one)
    monkeypatch.setattr(refresh_mod, "get_session", FakeSessionCM(account))

    await refresh_mod._maybe_refresh_one(connection_id, ig_user_id, tenant_id)

    assert calls["n"] == 1, "no debe reintentar en el mismo tick"
    assert account.status == "degraded"
    assert "revocado" in account.status_reason

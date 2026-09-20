"""`check_recent_instagram_webhook`: detecta correctamente el caso "toggle
apagado" (sin webhooks recientes) vs. "funciona" (un WebhookEvent reciente
para esa cuenta)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import core.db.session as session_mod
import core.gateway.webhooks as webhooks_mod


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *a, **k):
        return _ScalarsResult(self._rows)


class FakeSessionCM:
    def __init__(self, rows):
        self._rows = rows

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeSession(self._rows)

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_sin_webhooks_recientes_simula_toggle_apagado(monkeypatch):
    """Sin ninguna suscripción activa (simulado: cero filas recientes en el
    log), la prueba de humo debe reportar que NO llegó nada."""
    monkeypatch.setattr(session_mod, "get_session", FakeSessionCM([]))

    llego = await webhooks_mod.check_recent_instagram_webhook("ig-user-1")
    assert llego is False


@pytest.mark.asyncio
async def test_con_webhook_reciente_detecta_que_funciona(monkeypatch):
    row = SimpleNamespace(payload={"entry_id": "ig-user-1", "external_id": "mid.123"}, received_at=datetime.now(UTC))
    monkeypatch.setattr(session_mod, "get_session", FakeSessionCM([row]))

    llego = await webhooks_mod.check_recent_instagram_webhook("ig-user-1")
    assert llego is True


@pytest.mark.asyncio
async def test_webhook_de_otra_cuenta_no_cuenta(monkeypatch):
    row = SimpleNamespace(payload={"entry_id": "ig-user-OTRA"}, received_at=datetime.now(UTC))
    monkeypatch.setattr(session_mod, "get_session", FakeSessionCM([row]))

    llego = await webhooks_mod.check_recent_instagram_webhook("ig-user-1")
    assert llego is False


@pytest.mark.asyncio
async def test_respeta_la_ventana_de_tiempo(monkeypatch):
    """El query real filtra por `received_at`, no esta función -- se
    verifica que el parámetro `within_minutes` se puede ajustar (usado por
    el default de 5 min de la prueba de humo)."""
    calls = []

    class SpySession(FakeSession):
        async def execute(self, *a, **k):
            calls.append(a)
            return await super().execute(*a, **k)

    class SpySessionCM(FakeSessionCM):
        async def __aenter__(self):
            return SpySession(self._rows)

    monkeypatch.setattr(session_mod, "get_session", SpySessionCM([]))
    await webhooks_mod.check_recent_instagram_webhook("ig-user-1", within_minutes=1)
    assert len(calls) == 1

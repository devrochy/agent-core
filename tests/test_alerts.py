"""`send_alert`: nunca lanza si el POST a Slack falla; siempre loguea; sin
webhook configurado, no intenta ningún POST."""

from __future__ import annotations

import httpx
import pytest

import core.shared.alerts as alerts_mod
from core.shared.alerts import send_alert


@pytest.mark.asyncio
async def test_sin_webhook_configurado_no_intenta_post(monkeypatch):
    monkeypatch.setattr(alerts_mod, "get_settings", lambda: type("S", (), {"slack_alerts_webhook_url": ""})())

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe intentar POST sin webhook configurado")

    monkeypatch.setattr(httpx, "AsyncClient", _debe_no_llamarse)

    await send_alert("Título", "Detalle", severity="warning")


@pytest.mark.asyncio
async def test_post_exitoso_no_lanza(monkeypatch):
    monkeypatch.setattr(
        alerts_mod, "get_settings", lambda: type("S", (), {"slack_alerts_webhook_url": "https://hooks.slack.test/x"})()
    )

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    await send_alert("Título", "Detalle", severity="critical")


@pytest.mark.asyncio
async def test_post_fallido_nunca_lanza(monkeypatch):
    monkeypatch.setattr(
        alerts_mod, "get_settings", lambda: type("S", (), {"slack_alerts_webhook_url": "https://hooks.slack.test/x"})()
    )

    class FakeClient:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            raise httpx.ConnectError("no se pudo conectar")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    await send_alert("Título", "Detalle", severity="info")

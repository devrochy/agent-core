"""Más de 5 firmas inválidas en 5 min dispara la alerta EXACTAMENTE una vez
(no una por cada intento extra)."""

from __future__ import annotations

import pytest

from core.gateway.webhook_verify import _INVALID_SIG_ALERT_THRESHOLD, record_invalid_signature


class FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.expired: list[str] = []

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expired.append(key)


@pytest.mark.asyncio
async def test_alerta_dispara_exactamente_en_el_umbral(monkeypatch):
    import core.gateway.webhooks as webhooks_mod

    fake = FakeRedis()
    monkeypatch.setattr("core.gateway.webhook_verify.get_redis", lambda: fake)

    alerts = []

    async def _fake_alert(title, detail, severity="warning"):
        alerts.append((title, severity))

    monkeypatch.setattr("core.shared.alerts.send_alert", _fake_alert)

    for _ in range(_INVALID_SIG_ALERT_THRESHOLD + 3):
        await webhooks_mod._alert_on_invalid_signature("app-1")

    assert len(alerts) == 1
    assert alerts[0][1] == "critical"


@pytest.mark.asyncio
async def test_incr_arma_expire_solo_en_el_primer_intento():
    fake = FakeRedis()
    import unittest.mock as mock

    import core.gateway.webhook_verify as verify_mod

    with mock.patch.object(verify_mod, "get_redis", lambda: fake):
        await record_invalid_signature("app-1")
        await record_invalid_signature("app-1")

    assert fake.expired == ["invalid_sig:app-1"]

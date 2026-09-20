"""`_subscribe_waba_to_app`: `POST /{waba_id}/subscribed_apps` + verificación
OBLIGATORIA vía GET inmediatamente después. Sin la verificación, un
`{"success": true}` del POST no garantiza nada -- el paso debe fallar
explícitamente si el GET devuelve `data` vacío."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_onboarding as onboarding_mod
from core.gateway.meta_onboarding import WizardError


class FakeSession:
    def __init__(self, waba):
        self._waba = waba

    async def execute(self, *a, **k):
        class _Result:
            def __init__(self, row):
                self._row = row

            def scalar_one_or_none(self):
                return self._row

        return _Result(self._waba)


class FakeSessionCM:
    def __init__(self, waba):
        self._waba = waba

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeSession(self._waba)

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_verificacion_exitosa_marca_subscribed_true(monkeypatch):
    waba = SimpleNamespace(subscribed=False)
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(waba))

    calls = []

    async def _fake_graph_fetch(path, *, method="GET", **kwargs):
        calls.append((path, method))
        if method == "POST":
            return {"success": True}
        return {"data": [{"whatsapp_business_api_data": {"id": "app-1"}}]}

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    await onboarding_mod._subscribe_waba_to_app("tok", "waba-1", "v21.0", uuid.uuid4(), "s3cr3t")

    assert calls == [("/waba-1/subscribed_apps", "POST"), ("/waba-1/subscribed_apps", "GET")]
    assert waba.subscribed is True


@pytest.mark.asyncio
async def test_data_vacio_falla_el_paso_y_marca_subscribed_false(monkeypatch):
    waba = SimpleNamespace(subscribed=False)
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(waba))

    async def _fake_graph_fetch(path, *, method="GET", **kwargs):
        if method == "POST":
            return {"success": True}
        return {"data": []}

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    with pytest.raises(WizardError, match="no confirmó"):
        await onboarding_mod._subscribe_waba_to_app("tok", "waba-1", "v21.0", uuid.uuid4(), "s3cr3t")
    assert waba.subscribed is False


@pytest.mark.asyncio
async def test_override_callback_uri_mayor_a_200_caracteres_rechaza(monkeypatch):
    with pytest.raises(WizardError, match="200"):
        await onboarding_mod._subscribe_waba_to_app(
            "tok", "waba-1", "v21.0", uuid.uuid4(), "s3cr3t", override_callback_uri="x" * 201
        )


@pytest.mark.asyncio
async def test_override_se_manda_en_el_body_del_post(monkeypatch):
    waba = SimpleNamespace(subscribed=False)
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(waba))
    captured = {}

    async def _fake_graph_fetch(path, *, method="GET", body=None, **kwargs):
        if method == "POST":
            captured["body"] = body
            return {"success": True}
        return {"data": [{"x": 1}]}

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    await onboarding_mod._subscribe_waba_to_app(
        "tok", "waba-1", "v21.0", uuid.uuid4(), "s3cr3t",
        override_callback_uri="https://panel.example.test/wh/acme", override_verify_token="tok-verify",
    )
    assert captured["body"] == {
        "override_callback_uri": "https://panel.example.test/wh/acme", "verify_token": "tok-verify"
    }

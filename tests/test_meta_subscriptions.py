"""Registro/verificación de suscripciones a nivel de app de Meta.

`upsert_app_subscription`/`delete_app_subscription` pasan por el cliente
único (`graph_fetch`), que escribe en `graph_api_calls`/`graph_api_usage`
-- se mockea directamente para no tocar la BD desde un test unitario, mismo
estilo que `tests/test_meta_preflight.py`. La persistencia local
(`persist_subscription_state`) sí toca la BD -- se mockea también, salvo en
los tests de `tests/integration/` que la ejercitan de verdad."""

from __future__ import annotations

import pytest

import core.gateway.meta_subscriptions as subs_mod
from core.gateway.meta_errors import GraphError
from core.gateway.meta_preflight import AppCredentials
from core.gateway.meta_subscriptions import (
    OBJECT_FIELDS,
    OBJECT_TO_CHANNEL,
    SubscriptionError,
    delete_app_subscription,
    humanize_subscription_error,
    upsert_app_subscription,
)

APP_SECRET = "s3cr3t-de-prueba-que-nunca-debe-aparecer"
ACCESS_TOKEN = "token-de-prueba-que-nunca-debe-aparecer"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, handler):
        self._handler = handler

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kwargs):
        return self._handler(url, params or {})


def _config():
    return AppCredentials(app_id="123456789", app_secret=APP_SECRET, config_id="", graph_api_version="v21.0")


def _oauth_success(url, params):
    if "oauth/access_token" in url:
        return FakeResponse(json_data={"access_token": ACCESS_TOKEN})
    raise AssertionError(f"URL no esperada en oauth: {url}")


async def _noop_persist(*a, **k):
    class _Row:
        meta_app_id = "123456789"
        object = "whatsapp_business_account"
        callback_url = k.get("callback_url")
        active = k.get("active")
        fields = k.get("fields", [])
        last_checked_at = None
        last_error = k.get("last_error")

    return _Row()


def test_object_fields_exactos():
    assert set(OBJECT_FIELDS["whatsapp_business_account"]) == {
        "messages",
        "message_template_status_update",
        "message_template_quality_update",
        "template_category_update",
        "phone_number_quality_update",
        "phone_number_name_update",
        "account_update",
        "account_alerts",
    }
    assert set(OBJECT_FIELDS["page"]) == {
        "messages",
        "messaging_postbacks",
        "messaging_optins",
        "message_deliveries",
        "message_reads",
        "messaging_referrals",
        "message_echoes",
        "messaging_handovers",
    }
    assert set(OBJECT_FIELDS["instagram"]) == {
        "messages",
        "messaging_postbacks",
        "messaging_seen",
        "message_reactions",
        "messaging_referral",
        "comments",
        "mentions",
    }
    assert set(OBJECT_TO_CHANNEL) == set(OBJECT_FIELDS)


@pytest.mark.asyncio
async def test_upsert_aborta_antes_de_llamar_a_meta_si_self_check_falla(monkeypatch):
    """Criterio de aceptación explícito: si nuestro propio callback no
    responde el challenge, se aborta con un mensaje claro ANTES de pegarle a
    Meta -- se verifica que `graph_fetch` nunca se llama."""

    def fake_httpx_client(handler):
        return FakeAsyncClient(handler)

    monkeypatch.setattr(
        subs_mod.httpx,
        "AsyncClient",
        FakeAsyncClient(lambda url, params: FakeResponse(status_code=200, text="challenge-equivocado")),
    )

    async def _graph_fetch_no_deberia_llamarse(*a, **k):
        raise AssertionError("graph_fetch no debe llamarse si el self-check falla")

    monkeypatch.setattr(subs_mod, "graph_fetch", _graph_fetch_no_deberia_llamarse)

    with pytest.raises(SubscriptionError):
        await upsert_app_subscription(
            _config(), "whatsapp_business_account",
            callback_url="https://example.test/webhooks/whatsapp", verify_token="tok",
        )


@pytest.mark.asyncio
async def test_upsert_object_desconocido_no_llama_a_nada(monkeypatch):
    async def _no_debe_llamarse(*a, **k):
        raise AssertionError("no debe intentar nada con un object inválido")

    monkeypatch.setattr(subs_mod.httpx, "AsyncClient", FakeAsyncClient(_no_debe_llamarse))
    monkeypatch.setattr(subs_mod, "graph_fetch", _no_debe_llamarse)

    with pytest.raises(SubscriptionError):
        await upsert_app_subscription(
            _config(), "objeto_inventado", callback_url="https://example.test/webhooks/whatsapp", verify_token="tok"
        )


@pytest.mark.asyncio
async def test_upsert_camino_feliz_incluye_include_values_true(monkeypatch):
    monkeypatch.setattr(
        subs_mod.httpx,
        "AsyncClient",
        FakeAsyncClient(lambda url, params: FakeResponse(status_code=200, text=params.get("hub.challenge", ""))),
    )
    monkeypatch.setattr(subs_mod, "persist_subscription_state", _noop_persist)

    captured = {}

    async def _fake_graph_fetch(path, **kwargs):
        captured.update(kwargs)
        captured["path"] = path
        return {"success": True}

    monkeypatch.setattr(subs_mod, "graph_fetch", _fake_graph_fetch)

    status = await upsert_app_subscription(
        _config(), "whatsapp_business_account",
        callback_url="https://example.test/webhooks/whatsapp", verify_token="tok-verify",
    )

    assert captured["path"] == "/123456789/subscriptions"
    assert captured["method"] == "POST"
    assert captured["query"]["include_values"] == "true"
    assert captured["query"]["object"] == "whatsapp_business_account"
    assert captured["query"]["callback_url"] == "https://example.test/webhooks/whatsapp"
    assert captured["query"]["verify_token"] == "tok-verify"
    assert set(captured["query"]["fields"].split(",")) == set(OBJECT_FIELDS["whatsapp_business_account"])
    assert status.active is True


@pytest.mark.asyncio
async def test_upsert_graph_error_persiste_error_humanizado_y_relanza(monkeypatch):
    monkeypatch.setattr(
        subs_mod.httpx,
        "AsyncClient",
        FakeAsyncClient(lambda url, params: FakeResponse(status_code=200, text=params.get("hub.challenge", ""))),
    )

    persisted = {}

    async def _capture_persist(meta_app_id, object, **kwargs):
        persisted.update(kwargs)
        return await _noop_persist(meta_app_id, object, **kwargs)

    monkeypatch.setattr(subs_mod, "persist_subscription_state", _capture_persist)

    async def _fake_graph_fetch(*a, **k):
        raise GraphError("Invalid OAuth access token", category="AUTH", http_status=401, code=190)

    monkeypatch.setattr(subs_mod, "graph_fetch", _fake_graph_fetch)

    with pytest.raises(GraphError):
        await upsert_app_subscription(
            _config(), "page", callback_url="https://example.test/webhooks/whatsapp", verify_token="tok"
        )
    assert "App Secret" in persisted["last_error"]


def test_delete_sin_object_es_type_error():
    with pytest.raises(TypeError):
        delete_app_subscription(_config())  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_delete_con_object_vacio_es_value_error():
    with pytest.raises(ValueError, match="object"):
        await delete_app_subscription(_config(), object="")


@pytest.mark.asyncio
async def test_delete_siempre_manda_object_a_meta(monkeypatch):
    monkeypatch.setattr(
        subs_mod.httpx,
        "AsyncClient",
        FakeAsyncClient(_oauth_success),
    )
    captured = {}

    async def _fake_graph_fetch(path, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(subs_mod, "graph_fetch", _fake_graph_fetch)


    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(subs_mod, "get_session", lambda: _FakeSession())

    await delete_app_subscription(_config(), object="instagram")
    assert captured["method"] == "DELETE"
    assert captured["query"] == {"object": "instagram"}


@pytest.mark.parametrize(
    "message,category,retry_after_ms,expected_fragment",
    [
        ("Invalid callback URI format", "PERMANENT", None, "HTTPS"),
        ("Invalid OAuth access token", "AUTH", None, "App Secret"),
        ("Webhook callback URL is not reachable or invalid", "PERMANENT", None, "callback_url"),
        ("App request limit reached", "RATE_LIMIT", 30_000, "30s"),
    ],
)
def test_humanize_subscription_error_casos_del_ticket(message, category, retry_after_ms, expected_fragment):
    exc = GraphError(message, category=category, retry_after_ms=retry_after_ms)
    assert expected_fragment in humanize_subscription_error(exc)

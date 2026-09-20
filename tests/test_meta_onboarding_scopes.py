"""`_verify_token_scopes`: VERIFICACIÓN/FALLBACK del scope de WhatsApp vía
`/debug_token` -- nunca debe abortar el onboarding, acepta nombres de scope
alternativos y tolera `target_ids` ausente. Mockea `graph_fetch`/
`get_session` directo -- no toca BD/red real."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_onboarding as onboarding_mod


class FakeSession:
    def __init__(self, connection):
        self._connection = connection

    async def get(self, model, id_):
        return self._connection

    async def flush(self):
        return None


class FakeSessionCM:
    def __init__(self, connection):
        self._connection = connection

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeSession(self._connection)

    async def __aexit__(self, *a):
        return False


def _settings():
    return SimpleNamespace(meta_app_id="app-1", meta_app_secret="s3cr3t", graph_api_version="v21.0")


@pytest.mark.asyncio
async def test_acepta_nombre_de_scope_alternativo_2026(monkeypatch):
    """Evidencia de 2026: Meta puede devolver `whatsapp_business_manage_events`
    en vez de `whatsapp_business_management` -- ambos deben aceptarse."""
    connection = SimpleNamespace(health={})
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(connection))

    async def _fake_graph_fetch(path, **kwargs):
        assert path == "/debug_token"
        assert kwargs["query"] == {"input_token": "tok"}
        return {"data": {"scopes": ["whatsapp_business_manage_events"], "granular_scopes": []}}

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    await onboarding_mod._verify_token_scopes("tok", "v21.0", _settings(), uuid.uuid4(), "waba-1")
    assert connection.health["token_scope_check"]["has_whatsapp_scope"] is True


@pytest.mark.asyncio
async def test_target_ids_ausente_no_aborta(monkeypatch):
    connection = SimpleNamespace(health={})
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(connection))

    async def _fake_graph_fetch(path, **kwargs):
        return {"data": {"scopes": ["whatsapp_business_management"], "granular_scopes": []}}

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    # No debe levantar ninguna excepción.
    await onboarding_mod._verify_token_scopes("tok", "v21.0", _settings(), uuid.uuid4(), "waba-1")
    assert connection.health["token_scope_check"]["target_ids_present"] is False


@pytest.mark.asyncio
async def test_debug_token_con_campos_faltantes_no_rompe(monkeypatch):
    """Parseo 100% tolerante: la forma exacta del JSON no está verificada."""
    connection = SimpleNamespace(health={})
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(connection))

    async def _fake_graph_fetch(path, **kwargs):
        return {}  # respuesta vacía / inesperada

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    await onboarding_mod._verify_token_scopes("tok", "v21.0", _settings(), uuid.uuid4(), "waba-1")
    assert connection.health["token_scope_check"]["has_whatsapp_scope"] is False


@pytest.mark.asyncio
async def test_graph_error_en_debug_token_no_aborta(monkeypatch):
    from core.gateway.meta_errors import GraphError

    async def _fake_graph_fetch(path, **kwargs):
        raise GraphError("boom", category="PERMANENT")

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    # No debe propagar la excepción.
    await onboarding_mod._verify_token_scopes("tok", "v21.0", _settings(), uuid.uuid4(), "waba-1")

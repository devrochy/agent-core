"""`save_byok_connection`: verifica contra Graph antes de persistir (nunca
guarda a ciegas), crea MetaConnection+PhoneNumber+tokens con credenciales
válidas, y reentrante -- correrlo de nuevo con el mismo app_id actualiza
en vez de duplicar."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_byok as byok_mod
from core.gateway.meta_byok import CLOUD_API_ACCESS_TOKEN_KIND, ByokError, save_byok_connection


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def first(self):
        return self._row


class FakeSession:
    def __init__(self, existing_connection=None, existing_phone=None):
        self._existing_connection = existing_connection
        self._existing_phone = existing_phone
        self.added = []
        self._store = {}

    async def execute(self, stmt, *a, **k):
        # Primer execute pide MetaConnection existente, segundo PhoneNumber existente.
        if not hasattr(self, "_first_done"):
            self._first_done = True
            return _ScalarResult(self._existing_connection)
        return _ScalarResult(self._existing_phone)

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        return None

    async def get(self, model, id_):
        for obj in self.added:
            if getattr(obj, "id", None) == id_:
                return obj
        return self._existing_connection


class FakeSessionCM:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        return False


def _mock_verify_ok(monkeypatch, info=None):
    async def _fake_verify(phone_number_id, access_token, app_secret):
        return True, "Credenciales verificadas con Meta", info or {"display_phone_number": "+1 555", "verified_name": "Mi Negocio"}

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _fake_verify)


def _mock_verify_fail(monkeypatch, message="Meta rechazó las credenciales: token inválido"):
    async def _fake_verify(phone_number_id, access_token, app_secret):
        return False, message, {}

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _fake_verify)


def _mock_token_store(monkeypatch):
    put_calls = []

    async def _fake_put_token(connection_id, kind, subject_id, token, **kwargs):
        put_calls.append((connection_id, kind, subject_id, token))

    monkeypatch.setattr(byok_mod.meta_token_store, "put_token", _fake_put_token)
    return put_calls


@pytest.mark.asyncio
async def test_credenciales_invalidas_no_persiste_nada(monkeypatch):
    _mock_verify_fail(monkeypatch)
    put_calls = _mock_token_store(monkeypatch)
    session = FakeSession()
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(session))

    with pytest.raises(ByokError):
        await save_byok_connection(uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1")

    assert session.added == []
    assert put_calls == []


@pytest.mark.asyncio
async def test_credenciales_validas_crea_conexion_telefono_y_tokens(monkeypatch):
    _mock_verify_ok(monkeypatch)
    put_calls = _mock_token_store(monkeypatch)
    session = FakeSession()
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(session))

    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    connection = await save_byok_connection(tenant_id, admin_id, "app-1", "secret-1", "phone-1", "tok-1")

    assert connection is not None
    assert connection.status == "connected"
    assert connection.meta_app_id == "app-1"
    assert connection.connected_by == admin_id

    phone_rows = [obj for obj in session.added if hasattr(obj, "phone_number_id")]
    assert len(phone_rows) == 1
    assert phone_rows[0].phone_number_id == "phone-1"
    assert phone_rows[0].registered is True
    assert phone_rows[0].display_phone_number == "+1 555"

    kinds = {call[1] for call in put_calls}
    assert kinds == {"app_access", CLOUD_API_ACCESS_TOKEN_KIND}
    assert all(call[0] == connection.id for call in put_calls)


@pytest.mark.asyncio
async def test_fallo_guardando_token_marca_error_y_no_deja_connected(monkeypatch):
    """Hallazgo del auditor-multitenant: si `put_token` falla DESPUÉS de
    verificar credenciales válidas contra Graph (fallo de infra, no de
    credenciales), la conexión no debe quedar "connected" sin secretos
    reales -- debe quedar en "error" con el motivo."""
    _mock_verify_ok(monkeypatch)
    session = FakeSession()
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(session))

    async def _fake_put_token_falla(*a, **k):
        raise RuntimeError("corte de red simulado")

    monkeypatch.setattr(byok_mod.meta_token_store, "put_token", _fake_put_token_falla)

    with pytest.raises(RuntimeError):
        await save_byok_connection(uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1")

    connection_rows = [obj for obj in session.added if hasattr(obj, "meta_app_id")]
    assert len(connection_rows) == 1
    assert connection_rows[0].status == "error"
    assert "corte de red simulado" in connection_rows[0].status_reason


@pytest.mark.asyncio
async def test_reentrante_actualiza_conexion_existente_en_vez_de_duplicar(monkeypatch):
    _mock_verify_ok(monkeypatch)
    _mock_token_store(monkeypatch)

    tenant_id = uuid.uuid4()
    existing = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, channel="whatsapp", environment="sandbox", meta_app_id="app-1",
        status="draft", status_reason="algo viejo", connected_by=None, connected_at=None,
    )
    session = FakeSession(existing_connection=existing)
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(session))

    connection = await save_byok_connection(tenant_id, uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1")

    assert connection.id == existing.id
    assert connection.status == "connected"
    assert connection.status_reason is None
    # No se agregó una MetaConnection nueva -- solo el PhoneNumber.
    connection_rows = [obj for obj in session.added if obj is existing]
    assert connection_rows == []

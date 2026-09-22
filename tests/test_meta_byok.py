"""`save_byok_connection`: verifica contra Graph antes de persistir (nunca
guarda a ciegas), crea MetaConnection+PhoneNumber+WabaAccount+tokens con
credenciales válidas, suscribe la WABA a la app (sin esto Meta nunca manda
mensajes aunque el webhook esté bien configurado -- bug real encontrado en
vivo), y es reentrante -- correrlo de nuevo con el mismo app_id actualiza
en vez de duplicar."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_byok as byok_mod
from core.gateway.meta_byok import (
    CLOUD_API_ACCESS_TOKEN_KIND,
    WEBHOOK_VERIFY_TOKEN_KIND,
    ByokError,
    resolve_webhook_verify_token,
    save_byok_connection,
)


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalar_one(self):
        return self._row

    def scalars(self):
        return self

    def first(self):
        return self._row


class FakeSession:
    """Devuelve, en orden, una fila fake por cada `execute()` -- el primer
    `get_session()` de `save_byok_connection` hace 3 selects (MetaConnection,
    PhoneNumber, WabaAccount). Una vez agotada esa cola, cualquier `execute()`
    adicional (los selects de `MetaConnection` por tenant_id al cerrar la
    función) devuelve la `MetaConnection` ya conocida en `known` -- suficiente
    para un fake, no parsea el SQL real."""

    def __init__(self, queue, known, added):
        self._queue = queue
        self._known = known
        self.added = added

    async def execute(self, stmt, *a, **k):
        if self._queue:
            return _ScalarResult(self._queue.pop(0))
        meta_connection = next((obj for obj in self._known.values() if hasattr(obj, "meta_app_id")), None)
        return _ScalarResult(meta_connection)

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)
        self._known[obj.id] = obj

    async def flush(self):
        return None

    async def get(self, model, id_):
        return self._known.get(id_)


class FakeSessionCM:
    """Cada `async with get_session()` nuevo arranca una `FakeSession`
    fresca (mismo criterio que el código real: son sesiones/transacciones
    separadas), pero todas comparten `known`/`added` para poder inspeccionar
    el resultado final desde el test."""

    def __init__(self, shared_added, existing_connection=None, existing_phone=None, existing_waba=None, tenant=None):
        self._shared_added = shared_added
        self._known = {}
        for row in (existing_connection, existing_phone, existing_waba, tenant):
            if row is not None and getattr(row, "id", None) is not None:
                self._known[row.id] = row
        self._queue = [existing_connection, existing_phone, existing_waba]
        self._first_use = True

    def __call__(self):
        return self

    async def __aenter__(self):
        # Solo el PRIMER `get_session()` de la llamada hace las 3 queries
        # iniciales (connection/phone/waba) -- los siguientes ya resuelven
        # todo vía `.get()`/`known`.
        queue = self._queue if self._first_use else []
        self._first_use = False
        return FakeSession(queue, self._known, self._shared_added)

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


def _mock_subscribe_ok(monkeypatch):
    calls = []

    async def _fake_graph_fetch(path, *, method="GET", **kwargs):
        calls.append((path, method))
        if method == "POST":
            return {"success": True}
        return {"data": [{"whatsapp_business_api_data": {"id": "app-1"}}]}

    monkeypatch.setattr(byok_mod, "graph_fetch", _fake_graph_fetch)
    return calls


def _mock_subscribe_fail(monkeypatch):
    async def _fake_graph_fetch(path, *, method="GET", **kwargs):
        if method == "POST":
            return {"success": True}
        return {"data": []}  # Meta no confirma la suscripción.

    monkeypatch.setattr(byok_mod, "graph_fetch", _fake_graph_fetch)


@pytest.mark.asyncio
async def test_credenciales_invalidas_no_persiste_nada(monkeypatch):
    _mock_verify_fail(monkeypatch)
    put_calls = _mock_token_store(monkeypatch)
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added))

    with pytest.raises(ByokError):
        await save_byok_connection(uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1")

    assert added == []
    assert put_calls == []


@pytest.mark.asyncio
async def test_falta_waba_id_no_persiste_nada(monkeypatch):
    _mock_verify_ok(monkeypatch)
    put_calls = _mock_token_store(monkeypatch)
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added))

    with pytest.raises(ByokError):
        await save_byok_connection(uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "")

    assert added == []
    assert put_calls == []


@pytest.mark.asyncio
async def test_credenciales_validas_crea_conexion_telefono_waba_y_suscribe(monkeypatch):
    _mock_verify_ok(monkeypatch)
    put_calls = _mock_token_store(monkeypatch)
    graph_calls = _mock_subscribe_ok(monkeypatch)
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added))

    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    connection = await save_byok_connection(
        tenant_id, admin_id, "app-1", "secret-1", "phone-1", "tok-1", "waba-1",
    )

    assert connection is not None
    assert connection.status == "connected"
    assert connection.meta_app_id == "app-1"
    assert connection.connected_by == admin_id

    phone_rows = [obj for obj in added if hasattr(obj, "phone_number_id")]
    assert len(phone_rows) == 1
    assert phone_rows[0].phone_number_id == "phone-1"
    assert phone_rows[0].registered is True

    waba_rows = [obj for obj in added if hasattr(obj, "waba_id")]
    assert len(waba_rows) == 1
    assert waba_rows[0].waba_id == "waba-1"

    kinds = {call[1] for call in put_calls}
    assert kinds == {"app_access", CLOUD_API_ACCESS_TOKEN_KIND}
    assert all(call[0] == connection.id for call in put_calls)

    # POST /{waba_id}/subscribed_apps + GET de verificación.
    assert ("/waba-1/subscribed_apps", "POST") in graph_calls
    assert ("/waba-1/subscribed_apps", "GET") in graph_calls


@pytest.mark.asyncio
async def test_suscribe_tambien_los_campos_de_webhook_a_nivel_de_app(monkeypatch):
    """Bug real encontrado en vivo: la suscripción de OBJETO por WABA no
    alcanza -- si el admin configuró el webhook a mano en el App Dashboard
    sin tildar el checkbox de "messages", Meta la deja `active=true` pero
    sin ningún `fields`. `save_byok_connection` ahora la suscribe por API
    (mismo mecanismo que Embedded Signup), sin depender de ese checkbox."""
    from types import SimpleNamespace as NS

    _mock_verify_ok(monkeypatch)
    _mock_token_store(monkeypatch)
    _mock_subscribe_ok(monkeypatch)

    tenant_id = uuid.uuid4()
    tenant = NS(id=tenant_id, slug="mi-tenant")
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added, tenant=tenant))
    monkeypatch.setattr("core.tenants.tenant_webhook_url", lambda slug: f"https://x.test/webhooks/whatsapp/{slug}")

    calls = []

    async def _fake_upsert(config, object_, *, callback_url, verify_token):
        calls.append((config.app_id, object_, callback_url, verify_token))
        return NS()

    monkeypatch.setattr("core.gateway.meta_subscriptions.upsert_app_subscription", _fake_upsert)

    await save_byok_connection(tenant_id, uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1")

    assert len(calls) == 1
    app_id, object_, callback_url, _verify_token = calls[0]
    assert app_id == "app-1"
    assert object_ == "whatsapp_business_account"
    assert callback_url == "https://x.test/webhooks/whatsapp/mi-tenant"


@pytest.mark.asyncio
async def test_subscripcion_no_confirmada_marca_error(monkeypatch):
    """Si Meta acepta el POST pero el GET de verificación devuelve vacío
    (bug real encontrado en vivo: handshake OK, cero webhook_events porque
    faltaba este paso), la conexión no debe quedar "connected"."""
    _mock_verify_ok(monkeypatch)
    _mock_token_store(monkeypatch)
    _mock_subscribe_fail(monkeypatch)
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added))

    with pytest.raises(ByokError, match="no confirmó la suscripción"):
        await save_byok_connection(uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1")

    connection_rows = [obj for obj in added if hasattr(obj, "meta_app_id")]
    assert len(connection_rows) == 1
    assert connection_rows[0].status == "error"
    assert "suscripción" in connection_rows[0].status_reason


@pytest.mark.asyncio
async def test_graph_error_al_suscribir_se_envuelve_en_byok(monkeypatch):
    """`upsert_app_subscription` re-lanza el `GraphError` de Meta (p.ej. `#2200
    Callback verification failed`): debe salir como `ByokError` (flash en el
    panel), no propagar `GraphError` (que la ruta del wizard no captura -> 500)."""
    from core.gateway.meta_errors import GraphError

    _mock_verify_ok(monkeypatch)
    _mock_token_store(monkeypatch)
    _mock_subscribe_ok(monkeypatch)
    added: list = []
    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(id=tenant_id, slug="acme")
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added, tenant=tenant))

    async def _upsert_fail(*a, **k):
        raise GraphError("(#2200) Callback verification failed", category="PERMANENT")

    monkeypatch.setattr("core.gateway.meta_subscriptions.upsert_app_subscription", _upsert_fail)

    with pytest.raises(ByokError, match="No se pudo suscribir"):
        await save_byok_connection(
            tenant_id, uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1"
        )

    connection_rows = [obj for obj in added if hasattr(obj, "meta_app_id")]
    assert connection_rows and connection_rows[0].status == "error"


@pytest.mark.asyncio
async def test_verify_token_vacio_no_guarda_nada_nuevo(monkeypatch):
    """Dejar el campo vacío no debe cambiar el comportamiento actual --
    el handshake sigue cayendo al `.env` global."""
    _mock_verify_ok(monkeypatch)
    put_calls = _mock_token_store(monkeypatch)
    _mock_subscribe_ok(monkeypatch)
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added))

    await save_byok_connection(
        uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1", verify_token=None,
    )

    kinds = {call[1] for call in put_calls}
    assert WEBHOOK_VERIFY_TOKEN_KIND not in kinds


@pytest.mark.asyncio
async def test_verify_token_con_valor_se_guarda_con_el_kind_nuevo(monkeypatch):
    _mock_verify_ok(monkeypatch)
    put_calls = _mock_token_store(monkeypatch)
    _mock_subscribe_ok(monkeypatch)
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added))

    connection = await save_byok_connection(
        uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1",
        verify_token="mi-token-propio",
    )

    matching = [call for call in put_calls if call[1] == WEBHOOK_VERIFY_TOKEN_KIND]
    assert len(matching) == 1
    assert matching[0][0] == connection.id
    assert matching[0][3] == "mi-token-propio"


@pytest.mark.asyncio
async def test_resolve_webhook_verify_token_sin_conexion_conectada_da_none(monkeypatch):
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM([]))

    result = await resolve_webhook_verify_token(uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_webhook_verify_token_con_conexion_pero_sin_token_da_none(monkeypatch):
    connection = SimpleNamespace(id=uuid.uuid4(), meta_app_id="app-1")

    class _SingleQuerySession(FakeSession):
        def __init__(self):
            self._queue = [connection]
            self.added = []

    class _CM:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _SingleQuerySession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(byok_mod, "get_session", _CM())

    async def _fake_get_token(*a, **k):
        return None

    monkeypatch.setattr(byok_mod.meta_token_store, "get_token", _fake_get_token)

    result = await resolve_webhook_verify_token(uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_webhook_verify_token_con_token_guardado_lo_devuelve(monkeypatch):
    connection = SimpleNamespace(id=uuid.uuid4(), meta_app_id="app-1")

    class _SingleQuerySession(FakeSession):
        def __init__(self):
            self._queue = [connection]
            self.added = []

    class _CM:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _SingleQuerySession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(byok_mod, "get_session", _CM())

    async def _fake_get_token(connection_id, kind, subject_id, **kwargs):
        assert connection_id == connection.id
        assert kind == WEBHOOK_VERIFY_TOKEN_KIND
        assert subject_id == "app-1"
        return "mi-token-propio"

    monkeypatch.setattr(byok_mod.meta_token_store, "get_token", _fake_get_token)

    result = await resolve_webhook_verify_token(uuid.uuid4())
    assert result == "mi-token-propio"


@pytest.mark.asyncio
async def test_fallo_guardando_token_marca_error_y_no_deja_connected(monkeypatch):
    """Hallazgo del auditor-multitenant: si `put_token` falla DESPUÉS de
    verificar credenciales válidas contra Graph (fallo de infra, no de
    credenciales), la conexión no debe quedar "connected" sin secretos
    reales -- debe quedar en "error" con el motivo."""
    _mock_verify_ok(monkeypatch)
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added))

    async def _fake_put_token_falla(*a, **k):
        raise RuntimeError("corte de red simulado")

    monkeypatch.setattr(byok_mod.meta_token_store, "put_token", _fake_put_token_falla)

    with pytest.raises(RuntimeError):
        await save_byok_connection(uuid.uuid4(), uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1")

    connection_rows = [obj for obj in added if hasattr(obj, "meta_app_id")]
    assert len(connection_rows) == 1
    assert connection_rows[0].status == "error"
    assert "corte de red simulado" in connection_rows[0].status_reason


@pytest.mark.asyncio
async def test_reentrante_actualiza_conexion_existente_en_vez_de_duplicar(monkeypatch):
    _mock_verify_ok(monkeypatch)
    _mock_token_store(monkeypatch)
    _mock_subscribe_ok(monkeypatch)

    tenant_id = uuid.uuid4()
    existing = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, channel="whatsapp", environment="sandbox", meta_app_id="app-1",
        status="draft", status_reason="algo viejo", connected_by=None, connected_at=None,
    )
    added: list = []
    monkeypatch.setattr(byok_mod, "get_session", FakeSessionCM(added, existing_connection=existing))

    connection = await save_byok_connection(
        tenant_id, uuid.uuid4(), "app-1", "secret-1", "phone-1", "tok-1", "waba-1",
    )

    assert connection.id == existing.id
    assert connection.status == "connected"
    assert connection.status_reason is None
    # No se agregó una MetaConnection nueva -- solo PhoneNumber/WabaAccount.
    connection_rows = [obj for obj in added if obj is existing]
    assert connection_rows == []

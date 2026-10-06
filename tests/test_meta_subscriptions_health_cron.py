"""Cron de salud de suscripciones de app (core/tasks/meta_subscriptions_health.py).

Mockea `graph_fetch`/`fetch_app_access_token`/`persist_subscription_state`
y usa objetos `MetaConnection` en memoria (no instancias de sesión real) --
la lógica bajo test es la agrupación por app, la degradación de conexiones y
que un error en una app no interrumpa el resto."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

import core.tasks.meta_subscriptions_health as health_mod
from core.gateway.meta_errors import GraphError


@dataclass
class FakeConnection:
    id: uuid.UUID
    tenant_id: uuid.UUID
    meta_app_id: str
    channel: str
    status: str = "connected"
    graph_api_version: str = "v21.0"
    status_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _connection(**kwargs) -> FakeConnection:
    defaults = dict(id=uuid.uuid4(), tenant_id=uuid.uuid4(), meta_app_id="app-1", channel="whatsapp")
    defaults.update(kwargs)
    return FakeConnection(**defaults)


@pytest.mark.asyncio
async def test_active_false_degrada_solo_conexiones_de_ese_canal(monkeypatch):
    wa_conn = _connection(channel="whatsapp", status="connected")
    ig_conn = _connection(meta_app_id="app-1", channel="instagram", status="connected")

    async def _fake_token(*a, **k):
        return "token"

    async def _fake_graph_fetch(*a, **k):
        return {
            "data": [
                {"object": "whatsapp_business_account", "active": False, "fields": [], "callback_url": "x"},
                {"object": "instagram", "active": True, "fields": [], "callback_url": "y"},
            ]
        }

    persisted = []

    async def _fake_persist(meta_app_id, object_, **kwargs):
        persisted.append((meta_app_id, object_, kwargs))

    degraded = []

    async def _fake_degrade(connections, object_):
        for c in connections:
            if c.channel == health_mod.OBJECT_TO_CHANNEL.get(object_) and c.status == "connected":
                c.status = "degraded"
                degraded.append((c.id, object_))

    monkeypatch.setattr(health_mod, "fetch_app_access_token", _fake_token)
    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr(health_mod, "persist_subscription_state", _fake_persist)
    monkeypatch.setattr(health_mod, "_degrade_connections_for_object", _fake_degrade)

    group = health_mod._AppGroup(
        meta_app_id="app-1",
        app_secret="secret",
        graph_api_version="v21.0",
        representative_connection_id=wa_conn.id,
        representative_tenant_id=wa_conn.tenant_id,
        connections=[wa_conn, ig_conn],
    )
    await health_mod._check_group(group)

    assert (wa_conn.id, "whatsapp_business_account") in degraded
    assert not any(obj == "instagram" for _, obj in degraded)
    assert len(persisted) == 2


class _NoopCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["revoked", "degraded", "error"])
async def test_degrade_nunca_pisa_conexion_no_conectada(monkeypatch, status):
    """`_degrade_connections_for_object` filtra por `status == 'connected'`
    ANTES de tocar la BD -- una conexión `revoked` no debe entrar siquiera al
    bloque que abre sesión/tenant_scope. Tampoco una `degraded`/`error`, que desde
    ADR-0102 también están en los grupos."""
    revoked = _connection(channel="whatsapp", status=status)

    async def _debe_no_llamarse():
        raise AssertionError("no debe abrir sesión para una conexión que ya se filtró")

    monkeypatch.setattr(health_mod, "get_session", _debe_no_llamarse)
    monkeypatch.setattr(health_mod, "tenant_scope", lambda tid: _NoopCtx())

    await health_mod._degrade_connections_for_object([revoked], "whatsapp_business_account")
    assert revoked.status == status, "nunca debe pisar una conexión que no estaba 'connected'"


@pytest.mark.asyncio
async def test_una_app_caida_no_interrumpe_las_demas(monkeypatch):
    good = _connection(meta_app_id="app-ok")
    bad = _connection(meta_app_id="app-caida")

    async def _fake_group_connected_apps():
        return [
            health_mod._AppGroup(
                meta_app_id="app-caida", app_secret="s", graph_api_version="v21.0",
                representative_connection_id=bad.id, representative_tenant_id=bad.tenant_id, connections=[bad],
            ),
            health_mod._AppGroup(
                meta_app_id="app-ok", app_secret="s", graph_api_version="v21.0",
                representative_connection_id=good.id, representative_tenant_id=good.tenant_id, connections=[good],
            ),
        ]

    checked = []

    async def _fake_check_group(group):
        checked.append(group.meta_app_id)
        if group.meta_app_id == "app-caida":
            raise GraphError("boom", category="PERMANENT")

    monkeypatch.setattr(health_mod, "_group_connected_apps", _fake_group_connected_apps)
    monkeypatch.setattr(health_mod, "_check_group", _fake_check_group)

    await health_mod.check_all_subscriptions_health()
    assert checked == ["app-caida", "app-ok"], "la app caída no debe frenar el chequeo de la siguiente"


# --- `_group_connected_apps` (ADR-0102) ----------------------------------------------


def _patch_enumeracion(monkeypatch, connections, secrets, sin_numeros=()):
    """La BD en memoria: el enumerador, la hidratación, los números y los secretos."""
    pedidos: list[list[str]] = []
    por_id = {c.id: c for c in connections}

    async def _fake_enum(statuses):
        pedidos.append(list(statuses))
        return [(c.id, c.tenant_id) for c in connections if c.status in statuses]

    async def _fake_tiene_numeros(_session, connection_id, _tenant_id):
        return connection_id not in sin_numeros

    class _Session:
        async def get(self, _model, connection_id):
            return por_id.get(connection_id)

    @asynccontextmanager
    async def _fake_session():
        yield _Session()

    async def _fake_get_token(connection_id, kind, subject, *, tenant_id):
        assert kind == "app_access"
        return secrets.get(connection_id)

    monkeypatch.setattr("core.db.admin_enum.admin_meta_connection_ids", _fake_enum)
    monkeypatch.setattr("core.db.admin_enum.tiene_numeros", _fake_tiene_numeros)
    monkeypatch.setattr(health_mod, "get_session", _fake_session)
    monkeypatch.setattr(health_mod, "tenant_scope", lambda tid: _NoopCtx())
    monkeypatch.setattr(health_mod.meta_token_store, "get_token", _fake_get_token)
    return pedidos


@pytest.mark.asyncio
async def test_grupos_incluyen_apps_de_conexiones_degradadas_y_en_error(monkeypatch):
    """Una app con todas sus conexiones `degraded`/`error` se sigue revisando: si no,
    el `active=False` quedaba viejo y la conexión no se recuperaba nunca."""
    degradada = _connection(meta_app_id="app-d", status="degraded")
    en_error = _connection(meta_app_id="app-e", status="error")
    revocada = _connection(meta_app_id="app-r", status="revoked")
    pedidos = _patch_enumeracion(
        monkeypatch, [degradada, en_error, revocada], {c.id: "s" for c in (degradada, en_error, revocada)}
    )

    groups = await health_mod._group_connected_apps()

    assert pedidos == [["connected", "degraded", "error"]]
    assert sorted(g.meta_app_id for g in groups) == ["app-d", "app-e"]


@pytest.mark.asyncio
async def test_conexion_en_error_sin_numeros_no_entra(monkeypatch):
    """Un alta abandonada no se revisa (mismo criterio que el cron de salud)."""
    abandonada = _connection(meta_app_id="app-x", status="error")
    _patch_enumeracion(monkeypatch, [abandonada], {abandonada.id: "s"}, sin_numeros={abandonada.id})

    assert await health_mod._group_connected_apps() == []


@pytest.mark.asyncio
async def test_la_representativa_es_la_primera_sana_con_secreto(monkeypatch):
    """Antes era la más antigua y, si no tenía secreto, se saltaba toda la app."""
    ahora = datetime.now(UTC)
    vieja_sin_secreto = _connection(status="error", created_at=ahora - timedelta(days=30))
    degradada = _connection(status="degraded", created_at=ahora - timedelta(days=20))
    sana_sin_secreto = _connection(status="connected", created_at=ahora - timedelta(days=10))
    sana_nueva = _connection(status="connected", created_at=ahora)
    _patch_enumeracion(
        monkeypatch,
        [vieja_sin_secreto, degradada, sana_sin_secreto, sana_nueva],
        {degradada.id: "secreto", sana_nueva.id: "secreto"},
    )

    (group,) = await health_mod._group_connected_apps()

    assert group.representative_connection_id == sana_nueva.id
    assert group.app_secret == "secreto"
    assert [c.id for c in group.connections] == [sana_sin_secreto.id, sana_nueva.id, degradada.id, vieja_sin_secreto.id]


@pytest.mark.asyncio
async def test_sin_sanas_la_representativa_es_una_degradada_con_secreto(monkeypatch):
    ahora = datetime.now(UTC)
    vieja_sin_secreto = _connection(status="error", created_at=ahora - timedelta(days=30))
    degradada = _connection(status="degraded", created_at=ahora)
    _patch_enumeracion(monkeypatch, [vieja_sin_secreto, degradada], {degradada.id: "secreto"})

    (group,) = await health_mod._group_connected_apps()

    assert group.representative_connection_id == degradada.id


@pytest.mark.asyncio
async def test_app_sin_ningun_secreto_se_salta(monkeypatch):
    _patch_enumeracion(monkeypatch, [_connection(status="connected"), _connection(status="degraded")], {})

    assert await health_mod._group_connected_apps() == []

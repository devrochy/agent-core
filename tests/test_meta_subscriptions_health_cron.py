"""Cron de salud de suscripciones de app (core/tasks/meta_subscriptions_health.py).

Mockea `graph_fetch`/`fetch_app_access_token`/`persist_subscription_state`
y usa objetos `MetaConnection` en memoria (no instancias de sesión real) --
la lógica bajo test es la agrupación por app, la degradación de conexiones y
que un error en una app no interrumpa el resto."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

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
async def test_degrade_nunca_pisa_conexion_no_conectada(monkeypatch):
    """`_degrade_connections_for_object` filtra por `status == 'connected'`
    ANTES de tocar la BD -- una conexión `revoked` no debe entrar siquiera al
    bloque que abre sesión/tenant_scope."""
    revoked = _connection(channel="whatsapp", status="revoked")

    async def _debe_no_llamarse():
        raise AssertionError("no debe abrir sesión para una conexión que ya se filtró")

    monkeypatch.setattr(health_mod, "get_session", _debe_no_llamarse)
    monkeypatch.setattr(health_mod, "tenant_scope", lambda tid: _NoopCtx())

    await health_mod._degrade_connections_for_object([revoked], "whatsapp_business_account")
    assert revoked.status == "revoked", "nunca debe pisar una conexión que no estaba 'connected'"


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

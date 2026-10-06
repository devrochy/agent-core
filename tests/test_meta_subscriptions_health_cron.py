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
    deleted_at: datetime | None = None


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


def _patch_enumeracion(monkeypatch, connections, secrets, enumeradas=None):
    """La BD en memoria: el enumerador, la hidratación y los secretos. `enumeradas` simula
    lo que devolvió el enumerador antes de que el estado cambiara."""
    pedidos: list[list[str]] = []
    por_id = {c.id: c for c in connections}

    async def _fake_enum(statuses):
        pedidos.append(list(statuses))
        if enumeradas is not None:
            return [(c.id, c.tenant_id) for c in enumeradas]
        return [(c.id, c.tenant_id) for c in connections if c.status in statuses]

    class _Session:
        async def get(self, _model, connection_id):
            return por_id.get(connection_id)

    @asynccontextmanager
    async def _fake_session():
        yield _Session()

    async def _fake_get_token(connection_id, kind, subject, *, tenant_id):
        assert kind == "app_access"
        secret = secrets.get(connection_id)
        if isinstance(secret, Exception):
            raise secret
        return secret

    monkeypatch.setattr("core.db.admin_enum.admin_meta_connection_ids", _fake_enum)
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
async def test_una_conexion_que_cambio_de_estado_despues_de_enumerar_no_entra(monkeypatch):
    """Se enumeró `connected` y la desactivaron antes de hidratarla: no representa a la app
    ni tumba la corrida (antes, `_ORDEN_REPRESENTATIVA[status]` daba `KeyError`)."""
    desactivada = _connection(meta_app_id="app-x", status="revoked")
    sana = _connection(meta_app_id="app-x", status="connected")
    _patch_enumeracion(
        monkeypatch, [desactivada, sana], {desactivada.id: "s-vieja", sana.id: "s"}, enumeradas=[desactivada, sana]
    )

    (group,) = await health_mod._group_connected_apps()

    assert [c.id for c in group.connections] == [sana.id]
    assert group.representative_connection_id == sana.id


@pytest.mark.asyncio
async def test_un_secreto_ilegible_no_tumba_el_cron(monkeypatch):
    rota = _connection(meta_app_id="app-x", status="connected", created_at=datetime.now(UTC) - timedelta(days=1))
    sana = _connection(meta_app_id="app-x", status="connected")
    _patch_enumeracion(monkeypatch, [rota, sana], {rota.id: ValueError("no descifra"), sana.id: "s"})

    (group,) = await health_mod._group_connected_apps()

    assert group.representative_connection_id == sana.id


@pytest.mark.asyncio
async def test_los_respaldos_son_las_demas_con_un_secreto_distinto(monkeypatch):
    ahora = datetime.now(UTC)
    ajena = _connection(status="connected", created_at=ahora - timedelta(days=3))  # otro negocio, otro secreto
    misma = _connection(status="connected", created_at=ahora - timedelta(days=2))
    duena = _connection(status="degraded", created_at=ahora - timedelta(days=1))
    _patch_enumeracion(monkeypatch, [ajena, misma, duena], {ajena.id: "s-ajeno", misma.id: "s-ajeno", duena.id: "s-real"})

    (group,) = await health_mod._group_connected_apps()

    assert group.representative_connection_id == ajena.id
    assert group.respaldos == [(duena.id, duena.tenant_id, "s-real")]


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


def _grupo_con_respaldo():
    ajena, duena = _connection(status="connected"), _connection(status="degraded")
    group = health_mod._AppGroup(
        meta_app_id="app-1", app_secret="s-ajeno", graph_api_version="v21.0",
        representative_connection_id=ajena.id, representative_tenant_id=ajena.tenant_id,
        connections=[ajena, duena], respaldos=[(duena.id, duena.tenant_id, "s-real")],
    )
    return group, duena


@pytest.mark.asyncio
async def test_si_la_representativa_falla_se_prueba_el_respaldo(monkeypatch):
    """Un negocio cargó el `app_id` de otro con su propio secreto y quedó de representativa:
    antes la app quedaba "sin dato" y la conexión del dueño no se recuperaba nunca."""
    group, _duena = _grupo_con_respaldo()
    usados, persisted = [], []

    async def _fake_token(_app_id, app_secret, _version):
        usados.append(app_secret)
        if app_secret == "s-ajeno":
            raise GraphError("secreto de otra app", category="PERMANENT")
        return "token"

    async def _fake_graph_fetch(*_a, **_k):
        return {"data": [{"object": "whatsapp_business_account", "active": True, "fields": []}]}

    async def _fake_persist(meta_app_id, object_, **kwargs):
        persisted.append((object_, kwargs["active"]))

    monkeypatch.setattr(health_mod, "fetch_app_access_token", _fake_token)
    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr(health_mod, "persist_subscription_state", _fake_persist)
    monkeypatch.setattr(health_mod, "tenant_scope", lambda tid: _NoopCtx())

    await health_mod._check_group(group)

    assert usados == ["s-ajeno", "s-real"]
    assert persisted == [("whatsapp_business_account", True)]


@pytest.mark.asyncio
async def test_si_fallan_todas_la_app_queda_sin_dato(monkeypatch):
    group, _duena = _grupo_con_respaldo()
    persisted = []

    async def _fake_token(*_a, **_k):
        raise GraphError("Meta caída", category="TRANSIENT")

    async def _fake_persist(meta_app_id, object_, **kwargs):
        persisted.append((object_, kwargs["active"]))

    monkeypatch.setattr(health_mod, "fetch_app_access_token", _fake_token)
    monkeypatch.setattr(health_mod, "persist_subscription_state", _fake_persist)
    monkeypatch.setattr(health_mod, "tenant_scope", lambda tid: _NoopCtx())

    await health_mod._check_group(group)

    assert {active for _obj, active in persisted} == {None}
    assert len(persisted) == len(health_mod.OBJECT_TO_CHANNEL)

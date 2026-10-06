"""Credenciales de Meta por conexión con BD y token store reales (HU-CONN-01, ADR-0094).

Cubre lo que los unitarios (`tests/test_meta_credentials.py`) no pueden: que el
resolver lea del token store cifrado bajo RLS, que un negocio nunca resuelva las
credenciales de otro, que cada alta guarde su `origin`, que los crons usen las
credenciales de CADA conexión (y que una que falle no frene a las demás) y que el
backfill de la migración 0051 etiquete bien las conexiones existentes.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, stub_meta_pair_ok, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLATFORM_APP_ID = "platform-app-test"
PLATFORM_SECRET = "platform-secret-test"


# ------------------------------------------------------------------ helpers
async def _new_tenant(prefix: str) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de credenciales de Meta.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL,
    )
    return tid


async def _seed_connection(
    tid: uuid.UUID,
    *,
    origin: str,
    app_id: str,
    tokens: dict[tuple[str, str], str],
    business_id: str | None = None,
    with_waba: bool = True,
) -> tuple[uuid.UUID, str]:
    """Conexión `connected` con su WABA y los tokens dados. Devuelve (connection_id, waba_id)."""
    from core.db.models import MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    waba_id = uniq("waba")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin=origin, meta_app_id=app_id,
                business_id=business_id, status="connected",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
            if with_waba:
                session.add(WabaAccount(tenant_id=tid, connection_id=connection_id, waba_id=waba_id))
        for (kind, subject), token in tokens.items():
            await meta_token_store.put_token(connection_id, kind, subject, token, tenant_id=tid)
    return connection_id, waba_id


async def _load(tid: uuid.UUID, connection_id: uuid.UUID):
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await session.get(MetaConnection, connection_id)


@pytest.fixture
def platform_app(monkeypatch):
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", PLATFORM_APP_ID)
    monkeypatch.setattr(settings, "meta_app_secret", PLATFORM_SECRET)
    monkeypatch.setattr(settings, "meta_config_id", "platform-config")
    return settings


# ------------------------------------------------------------------ resolución real
async def test_credenciales_reales_segun_el_origen(platform_app) -> None:
    from core.gateway.meta_byok import APP_SECRET_KIND, CLOUD_API_ACCESS_TOKEN_KIND
    from core.gateway.meta_credentials import credentials_for
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("cred-origen")
    byok_id, byok_waba = await _seed_connection(
        tid, origin="byok", app_id="app-del-negocio",
        tokens={(CLOUD_API_ACCESS_TOKEN_KIND, "app-del-negocio"): "tok-byok", (APP_SECRET_KIND, "app-del-negocio"): "sec-byok"},
    )
    es_id, _ = await _seed_connection(
        tid, origin="embedded_signup", app_id=PLATFORM_APP_ID, business_id="biz-es",
        tokens={(TOKEN_KIND, "biz-es"): "tok-es"},
    )
    manual_id, _ = await _seed_connection(
        tid, origin="manual", app_id="app-manual", business_id="biz-manual",
        tokens={(TOKEN_KIND, "biz-manual"): "tok-manual"},
    )

    async with tenant_scope(tid):
        byok = await credentials_for(await _load(tid, byok_id))
        es = await credentials_for(await _load(tid, es_id))
        manual = await credentials_for(await _load(tid, manual_id))

    assert (byok.origin, byok.access_token, byok.app_secret, byok.app_id) == (
        "byok", "tok-byok", "sec-byok", "app-del-negocio",
    )
    assert byok.waba_id == byok_waba  # lo resuelve del WabaAccount si no se lo pasan
    assert (es.origin, es.access_token, es.app_secret, es.app_id) == (
        "embedded_signup", "tok-es", PLATFORM_SECRET, PLATFORM_APP_ID,
    )
    assert (manual.origin, manual.access_token, manual.app_secret) == ("manual", "tok-manual", PLATFORM_SECRET)


async def test_un_negocio_nunca_resuelve_las_credenciales_de_otro(platform_app) -> None:
    from core.gateway.meta_byok import APP_SECRET_KIND, CLOUD_API_ACCESS_TOKEN_KIND
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
    from core.shared.tenant import tenant_scope

    tid_a = await _new_tenant("cred-a")
    tid_b = await _new_tenant("cred-b")
    app_a, app_b = uniq("app-a"), uniq("app-b")
    conn_a, _ = await _seed_connection(
        tid_a, origin="byok", app_id=app_a,
        tokens={(CLOUD_API_ACCESS_TOKEN_KIND, app_a): "TOKEN-DE-A", (APP_SECRET_KIND, app_a): "SECRETO-DE-A"},
    )
    conn_b, _ = await _seed_connection(
        tid_b, origin="byok", app_id=app_b,
        tokens={(CLOUD_API_ACCESS_TOKEN_KIND, app_b): "TOKEN-DE-B", (APP_SECRET_KIND, app_b): "SECRETO-DE-B"},
    )

    # Cada negocio resuelve las suyas.
    async with tenant_scope(tid_a):
        creds_a = await credentials_for(await _load(tid_a, conn_a))
    async with tenant_scope(tid_b):
        creds_b = await credentials_for(await _load(tid_b, conn_b))
    assert (creds_a.access_token, creds_a.app_secret) == ("TOKEN-DE-A", "SECRETO-DE-A")
    assert (creds_b.access_token, creds_b.app_secret) == ("TOKEN-DE-B", "SECRETO-DE-B")

    connection_b = await _load(tid_b, conn_b)

    # 1. La conexión de B bajo el contexto de A se rechaza antes de leer nada.
    async with tenant_scope(tid_a):
        with pytest.raises(CredentialsUnavailable) as exc:
            await credentials_for(connection_b)
    assert exc.value.reason == "tenant_mismatch"

    # 2. Sin contexto de tenant falla cerrado ANTES de leer el token store: no se confía
    #    solo en RLS (en `meta_tokens` no está forzada) ni en el AAD.
    async with tenant_scope(None):
        with pytest.raises(CredentialsUnavailable) as exc:
            await credentials_for(connection_b)
    assert exc.value.reason == "no_tenant_scope"

    # 3. Conexión "forjada": el id y la app de B pero con el tenant de A. Aunque pase el
    #    chequeo de tenant, RLS no deja ver la fila de B bajo el contexto de A.
    from core.db.models import MetaConnection

    forjada = MetaConnection(
        id=conn_b, tenant_id=tid_a, channel="whatsapp", origin="byok", meta_app_id=app_b,
        graph_api_version="v26.0", deleted_at=None,
    )
    async with tenant_scope(tid_a):
        with pytest.raises(CredentialsUnavailable) as exc:
            await credentials_for(forjada)
    assert exc.value.reason == "missing_access_token"


# ------------------------------------------------------------------ cada alta guarda su origen
async def test_alta_byok_guarda_origen_y_el_resolver_lee_lo_recien_guardado(monkeypatch) -> None:
    import core.gateway.meta_byok as byok_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.gateway.meta_credentials import credentials_for
    from core.shared.tenant import tenant_scope

    async def verify_ok(phone_number_id, access_token, app_secret):
        return True, "ok", {"display_phone_number": "+1 555 0100", "verified_name": "Negocio Test"}

    async def graph_ok(path, *, method="GET", **kwargs):
        return {"success": True} if method == "POST" else {"data": [{"whatsapp_business_api_data": {"id": "x"}}]}

    async def sin_expiracion(*_a, **_k):
        return None

    async def upsert_ok(config, object_, *, callback_url, verify_token):
        return object()

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", verify_ok)
    stub_meta_pair_ok(monkeypatch)
    monkeypatch.setattr(byok_mod, "graph_fetch", graph_ok)
    monkeypatch.setattr(byok_mod, "_token_expiry", sin_expiracion)  # sin llamada real a Graph
    monkeypatch.setattr(subs_mod, "upsert_app_subscription", upsert_ok)

    tid = await _new_tenant("alta-byok")
    app_id = uniq("app-byok")
    async with tenant_scope(tid):
        saved = await byok_mod.save_byok_connection(
            tid, None, app_id, "secreto-del-wizard", uniq("phone"), "token-del-wizard", uniq("waba"),
            environment="production",
        )
        assert saved.origin == "byok"

        # Reconectar la misma app reutiliza la fila y la deja BYOK.
        again = await byok_mod.save_byok_connection(
            tid, None, app_id, "secreto-nuevo", uniq("phone"), "token-nuevo", uniq("waba"),
            environment="production",
        )
        assert again.id == saved.id and again.origin == "byok"

        creds = await credentials_for(await _load(tid, saved.id))
    assert (creds.origin, creds.access_token, creds.app_secret, creds.app_id) == (
        "byok", "token-nuevo", "secreto-nuevo", app_id,
    )


async def test_wizard_de_embedded_signup_crea_la_conexion_con_su_origen(platform_app) -> None:
    from core.gateway.meta_onboarding import start_or_resume_wizard
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("alta-es")
    async with tenant_scope(tid):
        wizard = await start_or_resume_wizard(tid)
    connection = await _load(tid, wizard.connection_id)

    assert connection.origin == "embedded_signup"
    assert connection.meta_app_id == PLATFORM_APP_ID


async def test_el_wizard_de_es_toma_una_conexion_viva_de_otro_origen(platform_app) -> None:
    """El wizard reutiliza una conexión viva; al guardar el token del system user pasa a ES."""
    import core.gateway.meta_onboarding as onboarding
    from core.db.models import ConnectionWizard
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("es-reusa")
    conn_id, _ = await _seed_connection(tid, origin="byok", app_id=uniq("app-byok"), tokens={}, with_waba=False)

    async with tenant_scope(tid):
        wizard = await onboarding.start_or_resume_wizard(tid)
    assert wizard.connection_id == conn_id  # reutilizó la conexión BYOK viva

    # El canje del code contra Meta se falsea: devuelve un token del system user.
    async def fake_exchange(version, params, settings):
        return "tok-system-user-nuevo", False

    original = onboarding._fetch_exchange_token
    onboarding._fetch_exchange_token = fake_exchange
    try:
        async with tenant_scope(tid):
            await onboarding.exchange_code(wizard.id, "code-de-un-solo-uso")
    finally:
        onboarding._fetch_exchange_token = original

    assert (await _load(tid, conn_id)).origin == "embedded_signup"
    async with tenant_scope(tid):
        async with get_session() as session:
            assert await session.get(ConnectionWizard, wizard.id) is not None


async def test_alta_manual_guarda_origen_y_resuelve_con_la_plataforma(app_client, monkeypatch, platform_app) -> None:
    import core.gateway.meta_health as health_mod
    from core.gateway.meta_credentials import credentials_for
    from core.shared.tenant import tenant_scope

    class _Ok:
        ok = True
        detail = "ok"

    async def fake_check(*_a, **_k):
        return _Ok()

    monkeypatch.setattr(health_mod, "check_token_valid", fake_check)
    stub_meta_pair_ok(monkeypatch)

    tid = await _new_tenant("alta-manual")
    headers = await api_headers(app_client, tid)
    waba_id = uniq("waba-manual")
    r = await app_client.post(
        "/api/v1/admin/meta/manual", headers=headers,
        json={"waba_id": waba_id, "phone_number_id": uniq("phone-manual"), "token": "tok-manual-api", "business_id": "biz-api"},
    )
    assert r.status_code == 201, r.text

    from core.db.models import MetaConnection
    from core.db.session import get_session

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = (
                await session.execute(select(MetaConnection).where(MetaConnection.tenant_id == tid))
            ).scalar_one()
        creds = await credentials_for(connection)
    assert connection.origin == "manual"
    # Se valida y se resuelve con la MISMA app (la de la plataforma).
    assert connection.meta_app_id == PLATFORM_APP_ID
    assert (creds.access_token, creds.app_secret) == ("tok-manual-api", PLATFORM_SECRET)


async def test_alta_manual_sin_app_de_plataforma_responde_claro(app_client, monkeypatch) -> None:
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "")
    monkeypatch.setattr(settings, "meta_app_secret", "")
    tid = await _new_tenant("manual-sin-app")
    headers = await api_headers(app_client, tid)

    r = await app_client.post(
        "/api/v1/admin/meta/manual", headers=headers,
        json={"waba_id": uniq("waba"), "phone_number_id": uniq("phone"), "token": "tok"},
    )

    assert r.status_code == 400
    assert "platform_app_not_configured" in r.text


async def test_extender_token_de_una_conexion_no_byok_responde_not_byok(app_client, platform_app) -> None:
    """Se valida el origen antes de resolver credenciales: `not_byok`, no `missing_credentials`."""
    tid = await _new_tenant("extend-es")
    es_id, _ = await _seed_connection(tid, origin="embedded_signup", app_id=PLATFORM_APP_ID, tokens={})
    headers = await api_headers(app_client, tid)

    r = await app_client.post(
        "/api/v1/admin/meta/byok/token/extend", headers=headers, json={"connection_id": str(es_id)}
    )

    assert r.status_code == 400
    assert "not_byok" in r.text


# ------------------------------------------------------------------ una sola regla: el origen
async def _taken_over_connection(platform_app) -> tuple[uuid.UUID, uuid.UUID]:
    """Conexión BYOK que el wizard de ES tomó: tiene los DOS tokens y `origin` = ES."""
    from datetime import UTC, datetime, timedelta

    from core.db.models import PhoneNumber
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import APP_SECRET_KIND, CLOUD_API_ACCESS_TOKEN_KIND
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("tomada")
    app = uniq("app-byok-vieja")
    conn_id, _ = await _seed_connection(
        tid, origin="embedded_signup", app_id=app, business_id="biz-tomada",
        tokens={(TOKEN_KIND, "biz-tomada"): "TOK-ES-VIGENTE", (APP_SECRET_KIND, app): "SEC-BYOK-VIEJO"},
    )
    async with tenant_scope(tid):
        # El token BYOK viejo, temporal y ya vencido.
        await meta_token_store.put_token(
            conn_id, CLOUD_API_ACCESS_TOKEN_KIND, app, "TOK-BYOK-VENCIDO", tenant_id=tid,
            expires_at=datetime.now(UTC) - timedelta(days=2),
        )
        async with get_session() as session:
            session.add(PhoneNumber(tenant_id=tid, connection_id=conn_id, phone_number_id=uniq("phone-tomada")))
    return tid, conn_id


async def test_el_sender_envia_con_el_token_que_dice_el_origen(platform_app) -> None:
    """Antes probaba "BYOK primero": salud en verde (token de ES) y envíos con el vencido."""
    from core.gateway.sender import _resolve_meta_connection_credentials
    from core.shared.tenant import tenant_scope

    tid, conn_id = await _taken_over_connection(platform_app)

    async with tenant_scope(tid):
        outbound = await _resolve_meta_connection_credentials(conn_id, tid)

    assert outbound is not None and outbound["access_token"] == "TOK-ES-VIGENTE"


async def test_el_cron_de_vencimiento_no_alerta_por_un_token_que_no_se_usa(monkeypatch, platform_app) -> None:
    import core.shared.alerts as alerts_mod
    from core.tasks.meta_token_expiry_check import check_tokens_expiry

    _tid, conn_id = await _taken_over_connection(platform_app)
    alertas: list[str] = []

    async def fake_alert(title, message, severity="warning"):
        alertas.append(message)

    monkeypatch.setattr(alerts_mod, "send_alert", fake_alert)

    await check_tokens_expiry()

    # El token BYOK vencido ya no se usa: ninguna alerta debe nombrar esta conexión.
    assert not [a for a in alertas if str(conn_id) in a]


async def test_el_cron_de_vencimiento_avisa_tambien_por_una_conexion_en_error(monkeypatch) -> None:
    """Un token a menos de 7 días de vencer ya pone la conexión en `error`. Desde ADR-0101 sigue
    recibiendo mensajes, así que el aviso de vencimiento no puede saltársela."""
    from datetime import UTC, datetime, timedelta

    import core.shared.alerts as alerts_mod
    from core.db.models import MetaConnection, PhoneNumber
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import CLOUD_API_ACCESS_TOKEN_KIND
    from core.shared.tenant import tenant_scope
    from core.tasks.meta_token_expiry_check import check_tokens_expiry

    async def conexion_en_error(con_numero: bool) -> uuid.UUID:
        tid = await _new_tenant("venc-error")
        app = uniq("app-venc")
        conn_id, _waba = await _seed_connection(tid, origin="byok", app_id=app, tokens={})
        async with tenant_scope(tid):
            await meta_token_store.put_token(
                conn_id, CLOUD_API_ACCESS_TOKEN_KIND, app, "TOK-POR-VENCER", tenant_id=tid,
                expires_at=datetime.now(UTC) + timedelta(days=2),
            )
            async with get_session() as session:
                (await session.get(MetaConnection, conn_id)).status = "error"
                if con_numero:
                    session.add(PhoneNumber(tenant_id=tid, connection_id=conn_id, phone_number_id=uniq("phone")))
        return conn_id

    con_numero = await conexion_en_error(True)
    abandonada = await conexion_en_error(False)  # alta fallida o reemplazada: no tiene números
    alertas: list[str] = []

    async def fake_alert(title, message, severity="warning"):
        alertas.append(message)

    monkeypatch.setattr(alerts_mod, "send_alert", fake_alert)

    await check_tokens_expiry()

    assert [a for a in alertas if str(con_numero) in a]
    assert not [a for a in alertas if str(abandonada) in a]


# ------------------------------------------------------------------ crons por conexión
async def _three_connections(platform_app):
    """BYOK completa, ES completa y BYOK sin App Secret (no utilizable), en negocios distintos."""
    from core.gateway.meta_byok import APP_SECRET_KIND, CLOUD_API_ACCESS_TOKEN_KIND
    from core.gateway.meta_onboarding import TOKEN_KIND

    tid_byok, tid_es, tid_roto = await _new_tenant("cron-byok"), await _new_tenant("cron-es"), await _new_tenant("cron-roto")
    app = uniq("app-cron")
    _byok_id, byok_waba = await _seed_connection(
        tid_byok, origin="byok", app_id=app,
        tokens={(CLOUD_API_ACCESS_TOKEN_KIND, app): "TOK-BYOK", (APP_SECRET_KIND, app): "SEC-BYOK"},
    )
    es_id, es_waba = await _seed_connection(
        tid_es, origin="embedded_signup", app_id=PLATFORM_APP_ID, business_id=uniq("biz"),
        tokens={},  # se completa abajo con el business_id
    )
    es_conn = await _load(tid_es, es_id)
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid_es):
        await meta_token_store.put_token(es_id, TOKEN_KIND, es_conn.business_id, "TOK-ES", tenant_id=tid_es)
    app_roto = uniq("app-roto")
    roto_id, roto_waba = await _seed_connection(
        tid_roto, origin="byok", app_id=app_roto,
        tokens={(CLOUD_API_ACCESS_TOKEN_KIND, app_roto): "TOK-SIN-SECRETO"},  # falta el App Secret
    )
    return {"byok": byok_waba, "es": es_waba, "roto": roto_waba}, {"roto": (tid_roto, roto_id)}


async def test_cron_de_plantillas_usa_las_credenciales_de_cada_conexion(monkeypatch, platform_app) -> None:
    import core.tasks.message_template_sync as cron
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_sync import acquire_sync_lock

    wabas, roto = await _three_connections(platform_app)
    llamadas: dict[str, tuple[str, str]] = {}

    async def fake_sync(waba_id, token, app_secret, version, connection_id, tenant_id):
        llamadas[waba_id] = (token, app_secret)
        if waba_id == wabas["byok"]:
            raise GraphError("Meta rechazó la sincronización de esta WABA", category="PERMANENT")

    monkeypatch.setattr(cron, "sync_templates", fake_sync)

    await cron.sync_all_waba_templates()  # nunca levanta, aunque una conexión falle

    # Cada conexión se sincronizó con SUS credenciales; la que falló no frenó a la de ES.
    assert llamadas[wabas["byok"]] == ("TOK-BYOK", "SEC-BYOK")
    assert llamadas[wabas["es"]] == ("TOK-ES", PLATFORM_SECRET)
    # La conexión sin credenciales utilizables se omite sin gastar el lock horario.
    assert wabas["roto"] not in llamadas
    assert await acquire_sync_lock(roto["roto"][0], wabas["roto"]) is True


async def test_cron_de_precios_usa_las_credenciales_de_cada_conexion(monkeypatch, platform_app) -> None:
    import core.tasks.meta_pricing_sync as cron
    from core.gateway.meta_errors import GraphError

    wabas, _ = await _three_connections(platform_app)
    llamadas: dict[str, tuple[str, str, str]] = {}

    async def fake_pricing(waba_id, waba_row_id, tenant_id, start, end, token, app_secret, version, connection_id):
        llamadas[waba_id] = (token, app_secret, version)
        if waba_id == wabas["byok"]:
            raise GraphError("Meta rechazó la consulta de precios", category="PERMANENT")

    monkeypatch.setattr(cron, "sync_pricing_daily", fake_pricing)

    await cron.sync_all_waba_pricing()

    assert llamadas[wabas["byok"]][:2] == ("TOK-BYOK", "SEC-BYOK")
    assert llamadas[wabas["es"]][:2] == ("TOK-ES", PLATFORM_SECRET)
    assert wabas["roto"] not in llamadas


async def test_sync_manual_de_precios_reporta_la_conexion_sin_credenciales(monkeypatch, platform_app) -> None:
    import core.gateway.meta_pricing_analytics as analytics
    from core.admin.meta_costs_service import sync_meta_pricing

    wabas, roto = await _three_connections(platform_app)
    tid_roto, _ = roto["roto"]
    llamadas: list[str] = []

    async def fake_pricing(waba_id, *a, **k):
        llamadas.append(waba_id)

    monkeypatch.setattr(analytics, "sync_pricing_daily", fake_pricing)

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid_roto):  # el handler real ya corre bajo el tenant del admin
        errores = await sync_meta_pricing(tid_roto)

    assert llamadas == []
    assert len(errores) == 1 and wabas["roto"] in errores[0]  # antes se omitía en silencio


# ------------------------------------------------------------------ backfill de la migración 0051
def _migration_0051():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0051_meta_connections_origin.py"
    spec = importlib.util.spec_from_file_location("migracion_0051", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_backfill_de_la_migracion_etiqueta_cada_conexion_segun_su_evidencia() -> None:
    """Corre el SQL REAL del backfill sobre conexiones sembradas con la evidencia de cada
    alta, como `app_runtime` (rol sin bypass, sujeto a RLS): así prueba lo que justifica
    recorrer tenant por tenant. Todo dentro de una transacción que se revierte."""
    import sqlalchemy as sa

    from core.config import get_settings

    settings = get_settings()
    if not settings.migrations_database_url:
        pytest.skip("requiere MIGRATIONS_DATABASE_URL (rol dueño de las tablas) para sembrar y alterar la columna")
    engine = sa.create_engine(settings.migrations_database_url.replace("+asyncpg", "+psycopg2"))
    migracion = _migration_0051()
    tenant = uuid.uuid4()
    blob = {"c": b"\x00", "i": b"\x00", "a": b"\x00"}

    casos = {  # nombre -> (tokens, tiene_wizard, origen esperado)
        "byok_con_token_cloud": (["cloud_api_access_token", "app_access"], False, "byok"),
        "es_con_token_de_system_user": (["business_integration_system_user"], False, "embedded_signup"),
        "es_con_system_user_y_app_access": (["business_integration_system_user", "app_access"], False, "embedded_signup"),
        "es_solo_con_wizard": ([], True, "embedded_signup"),
        "byok_solo_con_app_access": (["app_access"], False, "byok"),
        "byok_revocada_solo_con_verify_token": (["webhook_verify_token"], False, "byok"),
        "cloud_pesa_mas_que_system_user": (["cloud_api_access_token", "business_integration_system_user"], False, "byok"),
        "sin_evidencia": ([], False, "manual"),
    }

    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                puede = conn.execute(sa.text(
                    "select pg_has_role(current_user, 'app_runtime', 'MEMBER') "
                    "or (select rolsuper from pg_roles where rolname = current_user)"
                )).scalar_one()
                if not puede:
                    pytest.skip("el rol dueño no puede asumir app_runtime en este entorno")

                # Como dueño: abrir la columna y sembrar.
                conn.execute(sa.text("alter table meta_connections alter column origin drop not null"))
                conn.execute(sa.text("select set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
                ids: dict[str, uuid.UUID] = {}
                for i, (nombre, (kinds, wizard, _)) in enumerate(casos.items()):
                    cid = ids[nombre] = uuid.uuid4()
                    conn.execute(
                        sa.text(
                            "insert into meta_connections (id, tenant_id, channel, environment, meta_app_id, status, origin) "
                            "values (:id, :t, 'whatsapp', 'production', :app, 'connected', null)"
                        ),
                        {"id": cid, "t": tenant, "app": f"app-backfill-{i}"},
                    )
                    for j, kind in enumerate(kinds):
                        conn.execute(
                            sa.text(
                                "insert into meta_private.meta_tokens "
                                "(id, tenant_id, connection_id, kind, subject_id, ciphertext, iv, auth_tag, key_id, token_hash) "
                                "values (gen_random_uuid(), :t, :c, :k, :s, :ct, :iv, :tag, 'k1', 'h')"
                            ),
                            {"t": tenant, "c": cid, "k": kind, "s": f"s{j}", "ct": blob["c"], "iv": blob["i"], "tag": blob["a"]},
                        )
                    if wizard:
                        conn.execute(
                            sa.text(
                                "insert into connection_wizards (id, tenant_id, connection_id, channel) "
                                "values (gen_random_uuid(), :t, :c, 'whatsapp')"
                            ),
                            {"t": tenant, "c": cid},
                        )

                # Como un rol SIN bypass: es el caso que el bucle por tenant protege.
                conn.execute(sa.text("set local role app_runtime"))

                # Sin el GUC, el mismo UPDATE no falla: no ve filas y deja todo sin origen.
                conn.execute(sa.text("select set_config('app.tenant_id', '', true)"))
                sin_guc = conn.execute(sa.text(migracion._BACKFILL)).rowcount

                # Con el GUC del tenant (lo que hace la migración), etiqueta cada conexión.
                conn.execute(sa.text("select set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
                ambas = {row[0] for row in conn.execute(sa.text(migracion._BOTH_FAMILIES))}
                con_guc = conn.execute(sa.text(migracion._BACKFILL)).rowcount
                resultado = {
                    nombre: conn.execute(
                        sa.text("select origin from meta_connections where id = :id"), {"id": cid}
                    ).scalar_one()
                    for nombre, cid in ids.items()
                }
            finally:
                trans.rollback()
    finally:
        engine.dispose()

    assert sin_guc == 0
    assert con_guc == len(casos)
    assert resultado == {nombre: esperado for nombre, (_, _, esperado) in casos.items()}
    # Las que tienen tokens de los dos orígenes se listan para revisarlas a mano.
    assert ambas == {ids["cloud_pesa_mas_que_system_user"]}

"""Checklist de producción real (HU-CONN-07).

- Por conexión: "Producción" de una conexión evalúa número registrado, WABA suscrita, webhook,
  plantillas mínimas aprobadas y token con las credenciales de ESA conexión.
- Plataforma: los checks de la app de Meta de la plataforma y la infraestructura solo los ve y
  corre el master.
- Sin bloqueo: el request solo encola (job de Celery) y el resultado queda guardado con fecha.

Meta no se llama: se falsean las funciones de `core.gateway.meta_health` que hablan con Graph.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "clave-segura-123"


async def _tenant() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("produccion")
    tid, _ = await onboard_tenant(
        name=f"Producción {slug}", slug=slug, persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL
    )
    return tid


async def _conexion_byok(
    tid: uuid.UUID, *, numeros: int = 1, plantillas: tuple[str, ...] = ("bienvenida",)
) -> tuple[uuid.UUID, str, str]:
    """Conexión BYOK con su app, token, WABA suscrita, números y plantillas aprobadas.
    Devuelve `(connection_id, app_id, app_secret)`."""
    from core.db.models import MessageTemplate, MetaConnection, PhoneNumber, WabaAccount, utcnow
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.shared.tenant import tenant_scope

    app_id = f"app-prod-{uuid.uuid4().hex[:8]}"
    app_secret = f"secreto-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="production", origin="byok", meta_app_id=app_id,
                status="connected", connected_at=utcnow(), health={},
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(
                tenant_id=tid, connection_id=connection.id, waba_id=f"waba-{uuid.uuid4().hex[:8]}", subscribed=True
            )
            session.add(waba)
            await session.flush()
            for i in range(numeros):
                session.add(
                    PhoneNumber(
                        tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id,
                        phone_number_id=f"ph-prod-{uuid.uuid4().hex[:10]}", display_phone_number=f"+57 300 000 00{i}",
                    )
                )
            for nombre in plantillas:
                session.add(
                    MessageTemplate(
                        tenant_id=tid, waba_account_id=waba.id, name=nombre, language="es", status="APPROVED",
                        category="UTILITY", components=[], synced_at=utcnow(),
                    )
                )
            cid = connection.id
        await meta_byok.meta_token_store.put_token(cid, meta_byok.APP_SECRET_KIND, app_id, app_secret, tenant_id=tid)
        await meta_byok.meta_token_store.put_token(
            cid, meta_byok.CLOUD_API_ACCESS_TOKEN_KIND, app_id, f"token-{app_id}", tenant_id=tid
        )
    return cid, app_id, app_secret


def _meta_falso(monkeypatch, *, token_ok: bool = True) -> list[tuple[str, str, str]]:
    """Falsea las llamadas a Graph de `meta_health`. Devuelve las credenciales con las que se
    llamó a `debug_token` (`(token, app_id, app_secret)`)."""
    import core.gateway.meta_health as mh
    from core.gateway.meta_preflight import PreflightCheck

    llamadas: list[tuple[str, str, str]] = []

    def _ok(key: str) -> PreflightCheck:
        return PreflightCheck(key=key, ok=True, detail="bien", checked_at=datetime.now(UTC))

    async def _token(token, app_id, app_secret, version, **_kw):
        llamadas.append((token, app_id, app_secret))
        if token_ok:
            return _ok("token_valid")
        return PreflightCheck(
            key="token_valid", ok=False, detail="vencido", checked_at=datetime.now(UTC), repair_action="reauthorize"
        )

    async def _numero(*_a, **_kw):
        return _ok("number_registered")

    async def _waba(*_a, **_kw):
        return _ok("webhook_subscribed")

    async def _override(*_a, **_kw):
        return _ok("webhook_override")

    async def _app(_connection):
        return _ok("app_subscription_active")

    monkeypatch.setattr(mh, "check_token_valid", _token)
    monkeypatch.setattr(mh, "check_number_registered", _numero)
    monkeypatch.setattr(mh, "check_webhook_subscribed", _waba)
    monkeypatch.setattr(mh, "check_webhook_override", _override)
    monkeypatch.setattr(mh, "check_app_subscription", _app)
    return llamadas


def _cola_falsa(monkeypatch) -> list[tuple]:
    """El job no va a Redis: se guarda lo encolado y el test lo ejecuta a mano (el worker)."""
    from core.tasks import production_readiness as tasks

    encolados: list[tuple] = []
    monkeypatch.setattr(tasks.run_connection_production_task, "delay", lambda *a: encolados.append(("conn", *a)))
    monkeypatch.setattr(tasks.run_platform_readiness_task, "delay", lambda *a: encolados.append(("plat", *a)))
    return encolados


async def _owner(tid: uuid.UUID) -> str:
    """Un dueño del negocio que NO es master."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('duenio')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return email


def _url(cid: uuid.UUID, *, run: bool = False) -> str:
    return f"/api/v1/admin/meta/connections/{cid}/production" + ("/run" if run else "")


# ----------------------------------------------------------------------- por conexión
@pytest.mark.aislamiento
async def test_produccion_de_una_conexion_se_encola_y_guarda_el_resultado_con_fecha(app_client, monkeypatch):
    from core.admin.production_readiness_service import execute_connection_run

    tid = await _tenant()
    cid, app_id, app_secret = await _conexion_byok(tid)
    headers = await api_headers(app_client, tid)
    encolados = _cola_falsa(monkeypatch)
    llamadas = _meta_falso(monkeypatch)

    r = await app_client.get(_url(cid), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "never"

    # El request solo encola: no llama a Meta.
    r = await app_client.post(_url(cid, run=True), headers=headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["requested_at"]
    assert llamadas == []
    assert encolados == [("conn", body["run_id"], str(tid))]

    # El worker la ejecuta con las credenciales de ESTA conexión.
    await execute_connection_run(uuid.UUID(body["run_id"]), tid)
    assert llamadas == [(f"token-{app_id}", app_id, app_secret)]

    r = await app_client.get(_url(cid), headers=headers)
    body = r.json()
    assert body["status"] == "done", body
    assert body["ready"] is True
    assert body["finished_at"]
    assert body["environment"] == "production"
    assert [i["key"] for i in body["items"]] == [
        "token", "number_registered", "waba_subscribed", "webhook", "templates_approved",
    ]
    assert all(i["ok"] is True and i["label"] and i["checked_at"] for i in body["items"])


@pytest.mark.aislamiento
async def test_cada_conexion_se_evalua_con_sus_propias_credenciales(app_client, monkeypatch):
    from core.admin.production_readiness_service import execute_connection_run

    tid = await _tenant()
    cid_a, _app_a, _secreto_a = await _conexion_byok(tid)
    cid_b, app_b, secreto_b = await _conexion_byok(tid)
    headers = await api_headers(app_client, tid)
    _cola_falsa(monkeypatch)
    llamadas = _meta_falso(monkeypatch)

    run_b = (await app_client.post(_url(cid_b, run=True), headers=headers)).json()["run_id"]
    await execute_connection_run(uuid.UUID(run_b), tid)

    assert llamadas == [(f"token-{app_b}", app_b, secreto_b)]
    assert (await app_client.get(_url(cid_a), headers=headers)).json()["status"] == "never"


@pytest.mark.aislamiento
async def test_otro_negocio_no_ve_ni_corre_la_produccion_de_una_conexion_ajena(app_client, monkeypatch):
    from sqlalchemy import select

    from core.admin.production_readiness_service import execute_connection_run
    from core.db.models import MetaProductionCheck
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid_a = await _tenant()
    tid_b = await _tenant()
    cid_a, _app, _secreto = await _conexion_byok(tid_a)
    encolados = _cola_falsa(monkeypatch)
    _meta_falso(monkeypatch)
    run_a = (await app_client.post(_url(cid_a, run=True), headers=await api_headers(app_client, tid_a))).json()
    await execute_connection_run(uuid.UUID(run_a["run_id"]), tid_a)
    encolados.clear()

    headers_b = await api_headers(app_client, tid_b, email=await _owner(tid_b), password=PW)
    r = await app_client.get(_url(cid_a), headers=headers_b)
    assert r.status_code == 404, r.text
    r = await app_client.post(_url(cid_a, run=True), headers=headers_b)
    assert r.status_code == 404, r.text
    assert encolados == []

    # El worker tampoco ejecuta una corrida con el negocio equivocado.
    await execute_connection_run(uuid.UUID(run_a["run_id"]), tid_b)

    # RLS: el negocio B no ve las corridas de A.
    async with tenant_scope(tid_b):
        async with get_session() as session:
            filas = (await session.execute(select(MetaProductionCheck.id))).scalars().all()
    assert uuid.UUID(run_a["run_id"]) not in filas


@pytest.mark.aislamiento
async def test_pedir_otra_corrida_mientras_una_esta_en_curso_devuelve_la_misma(app_client, monkeypatch):
    from sqlalchemy import update

    from core.db.models import MetaProductionCheck
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant()
    cid, _app, _secreto = await _conexion_byok(tid)
    headers = await api_headers(app_client, tid)
    encolados = _cola_falsa(monkeypatch)

    primera = (await app_client.post(_url(cid, run=True), headers=headers)).json()
    segunda = (await app_client.post(_url(cid, run=True), headers=headers)).json()
    assert segunda["run_id"] == primera["run_id"]
    assert len(encolados) == 1

    # Si el worker nunca la tomó, pasado el plazo se ve fallida y se puede pedir otra.
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaProductionCheck)
                .where(MetaProductionCheck.id == uuid.UUID(primera["run_id"]))
                .values(requested_at=datetime.now(UTC) - timedelta(hours=1))
            )
    vista = (await app_client.get(_url(cid), headers=headers)).json()
    assert vista["status"] == "failed" and vista["error"]
    tercera = (await app_client.post(_url(cid, run=True), headers=headers)).json()
    assert tercera["run_id"] != primera["run_id"]
    assert tercera["status"] == "queued"


async def test_sin_numeros_ni_plantillas_de_avisos_aprobadas_no_esta_lista(app_client, monkeypatch):
    from sqlalchemy import select

    from core.admin.production_readiness_service import execute_connection_run
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant()
    cid, _app, _secreto = await _conexion_byok(tid, numeros=0, plantillas=("bienvenida",))
    async with tenant_scope(tid):
        async with get_session() as session:
            ajustes = (await session.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one()
            ajustes.message_templates_map = {
                "subscription.charge_created": {"template": "cobro_nuevo", "language": "es"}
            }
    headers = await api_headers(app_client, tid)
    _cola_falsa(monkeypatch)
    _meta_falso(monkeypatch)

    run = (await app_client.post(_url(cid, run=True), headers=headers)).json()
    await execute_connection_run(uuid.UUID(run["run_id"]), tid)
    body = (await app_client.get(_url(cid), headers=headers)).json()

    items = {i["key"]: i for i in body["items"]}
    assert body["ready"] is False
    assert items["number_registered"]["ok"] is False
    assert items["templates_approved"]["ok"] is False
    assert "cobro_nuevo" in items["templates_approved"]["detail"]
    assert items["token"]["ok"] is True


async def test_conexion_sin_credenciales_falla_el_token_y_no_llama_a_meta(app_client, monkeypatch):
    from core.admin.production_readiness_service import execute_connection_run
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant()
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="production", origin="byok",
                meta_app_id=f"app-{uuid.uuid4().hex[:8]}", status="pending", connected_at=utcnow(), health={},
            )
            session.add(connection)
            await session.flush()
            cid = connection.id
    headers = await api_headers(app_client, tid)
    _cola_falsa(monkeypatch)
    llamadas = _meta_falso(monkeypatch)

    run = (await app_client.post(_url(cid, run=True), headers=headers)).json()
    await execute_connection_run(uuid.UUID(run["run_id"]), tid)
    body = (await app_client.get(_url(cid), headers=headers)).json()

    assert llamadas == []
    items = {i["key"]: i for i in body["items"]}
    assert items["token"]["ok"] is False
    assert body["ready"] is False


async def test_la_ruta_vieja_ya_no_corre_los_checks_de_plataforma(app_client, monkeypatch):
    """`/checklist/production/run` (app móvil) solo encola la revisión de cada conexión y
    devuelve lo guardado: nada de la plataforma ni ejecución en el request."""
    import core.gateway.meta_production_readiness as readiness_mod

    async def _prohibido(*_a, **_k):
        raise AssertionError("no debe correr checks de plataforma en el request")

    monkeypatch.setattr(readiness_mod, "run_platform_checks", _prohibido)
    monkeypatch.setattr(readiness_mod, "check_rls_forced", _prohibido)
    tid = await _tenant()
    await _conexion_byok(tid)
    encolados = _cola_falsa(monkeypatch)

    r = await app_client.post("/api/v1/admin/meta/checklist/production/run", headers=await api_headers(app_client, tid))

    assert r.status_code == 200, r.text
    assert [c["key"] for c in r.json()] == ["production_checks"]
    assert len(encolados) == 1


# ------------------------------------------------------------------------- plataforma
@pytest.mark.aislamiento
async def test_solo_el_master_ve_y_corre_la_app_de_meta_de_la_plataforma(app_client, monkeypatch):
    tid = await _tenant()
    encolados = _cola_falsa(monkeypatch)
    headers = await api_headers(app_client, tid, email=await _owner(tid), password=PW)

    assert (await app_client.get("/api/v1/admin/platform/meta-app", headers=headers)).status_code == 403
    assert (await app_client.post("/api/v1/admin/platform/meta-app/run", headers=headers)).status_code == 403
    assert encolados == []


@pytest.mark.aislamiento
async def test_la_corrida_de_plataforma_es_un_job_y_queda_guardada_con_fecha(app_client, monkeypatch):
    from sqlalchemy import update

    import core.gateway.meta_production_readiness as readiness_mod
    from core.admin.production_readiness_service import execute_platform_run
    from core.db.models import PlatformReadinessRun
    from core.db.session import get_session
    from core.gateway.meta_preflight import PreflightCheck

    # Una sola corrida activa en toda la base: se cierran las que hayan quedado de otros tests.
    async with get_session() as session:
        await session.execute(
            update(PlatformReadinessRun)
            .where(PlatformReadinessRun.status.in_(("queued", "running")))
            .values(status="failed", error="test")
        )
    tid = await _tenant()
    headers = await api_headers(app_client, tid)  # admin@local es master
    encolados = _cola_falsa(monkeypatch)
    corridas: list[int] = []

    async def _checks():
        corridas.append(1)
        return [
            PreflightCheck(key=key, ok=True, detail="bien", checked_at=datetime.now(UTC))
            for key in readiness_mod.PLATFORM_CHECK_LABELS
        ]

    monkeypatch.setattr(readiness_mod, "run_platform_checks", _checks)

    r = await app_client.post("/api/v1/admin/platform/meta-app/run", headers=headers)
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["status"] == "queued"
    assert corridas == []  # el request no corre nada
    assert encolados == [("plat", run["run_id"])]

    await execute_platform_run(uuid.UUID(run["run_id"]))

    body = (await app_client.get("/api/v1/admin/platform/meta-app", headers=headers)).json()
    assert corridas == [1]
    assert body["status"] == "done"
    assert body["ready"] is True
    assert body["finished_at"]
    assert [i["key"] for i in body["items"]] == list(readiness_mod.PLATFORM_CHECK_LABELS)

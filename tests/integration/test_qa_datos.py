"""Datos de prueba propios de cada tester (SPEC-30, HU-QA-07): negocio con Comercio, Agenda
y Cobranza, cuentas de dueño, gestor y colaborador, idempotencia, contraseñas nuevas y
solo en test."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers

pytestmark = [
    pytest.mark.aislamiento,  # crea cuentas y negocios: subconjunto de las PRs (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-del-tester-123"


@pytest.fixture
def testers_on(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)
    return monkeypatch


async def _tester(app_client, t1) -> tuple[dict[str, str], str]:
    """Una cuenta tester con contraseña, creada directo en la base."""
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    email = f"tester-datos-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        session.add(Admin(email=email, password_hash=hash_password(PASSWORD), role="tester", active=True))
    return await api_headers(app_client, t1, email=email, password=PASSWORD), email


async def test_preparar_mis_datos_crea_negocio_cuentas_y_datos(app_client, tenant_setup, testers_on) -> None:
    from pack_calendar.models import Appointment, AppointmentType
    from pack_cobranza.models import Cartera, Cliente, Cuota, Gestor, GestorPermiso
    from pack_commerce.models import Product
    from sqlalchemy import func, select

    from core.db.models import Tenant, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    headers, _email = await _tester(app_client, t1)

    r = await app_client.get("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["disponible"] is True and r.json()["datos"] is None
    assert {c["habilidad"] for c in r.json()["contenido"]} == {"Comercio", "Agenda", "Cobranza"}

    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    datos = r.json()
    assert datos["estado"] == "listo"
    assert set(datos["contrasenas"]) == {"dueno", "gestor", "colaborador"}
    assert all(c["creada"] and c["email"].endswith("@pruebas.solvershub.test") for c in datos["cuentas"])
    tid = uuid.UUID(datos["negocio"]["id"])

    # Las contraseñas no vuelven en la lectura.
    r = await app_client.get("/api/v1/admin/qa/datos", headers=headers)
    assert r.json()["datos"]["estado"] == "listo" and r.json()["datos"]["contrasenas"] is None

    async with get_session() as session:
        tenant = await session.get(Tenant, tid)
        assert tenant is not None and tenant.slug == datos["negocio"]["slug"]
        roles = dict(
            (await session.execute(select(TenantUser.admin_id, TenantUser.role).where(TenantUser.tenant_id == tid))).all()
        )
    assert sorted(roles.values()) == ["colaborador", "colaborador", "owner"]

    async with tenant_scope(tid):
        async with get_session() as session:
            assert (await session.execute(select(func.count()).select_from(Product).where(Product.tenant_id == tid))).scalar_one() == 4
            assert (await session.execute(select(func.count()).select_from(AppointmentType).where(AppointmentType.tenant_id == tid))).scalar_one() == 2
            assert (await session.execute(select(func.count()).select_from(Appointment).where(Appointment.tenant_id == tid))).scalar_one() == 2
            cartera = (await session.execute(select(Cartera).where(Cartera.tenant_id == tid))).scalar_one()
            gestor = (await session.execute(select(Gestor).where(Gestor.tenant_id == tid))).scalar_one()
            assert cartera.gestor_id == gestor.id
            assert (await session.execute(select(func.count()).select_from(GestorPermiso).where(GestorPermiso.gestor_id == gestor.id))).scalar_one() > 0
            assert (await session.execute(select(func.count()).select_from(Cliente).where(Cliente.tenant_id == tid))).scalar_one() == 4
            atrasadas = (await session.execute(select(func.count()).select_from(Cuota).where(Cuota.tenant_id == tid, Cuota.estatus == "atrasada"))).scalar_one()
            assert atrasadas == 9  # Juana Pérez en mora

    # El dueño entra al panel con su contraseña y ve su negocio.
    dueno = next(c for c in datos["cuentas"] if c["rol"] == "dueno")
    r = await app_client.post(
        "/api/v1/auth/login", json={"identifier": dueno["email"], "password": datos["contrasenas"]["dueno"]}
    )
    assert r.status_code == 200, r.text

    # El gestor entra a la app de campo y ve solo su cartera.
    gestor_cuenta = next(c for c in datos["cuentas"] if c["rol"] == "gestor")
    r = await app_client.post(
        "/api/pack/pack_cobranza/auth/login",
        # La app manda el id del equipo: sin él, un gestor en política restringida es 403 (ADR-0174).
        json={
            "usuario": gestor_cuenta["email"],
            "password": datos["contrasenas"]["gestor"],
            "imei": "IMEI-QA-DATOS",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json().get("accessToken"), r.json()

    # Idempotente: un segundo «Preparar» no crea otro negocio ni cambia las contraseñas; solo
    # pone al día el Agent OS (HU-QA-09; el ruteo por conexión es siempre, ADR-0191).
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["negocio"]["id"] == datos["negocio"]["id"] and r.json()["contrasenas"] is None
    assert r.json()["agent_os"]["estado"] == "listo"

    # Contraseñas nuevas: la vieja deja de servir y la nueva entra.
    r = await app_client.post("/api/v1/admin/qa/datos/contrasenas", headers=headers)
    assert r.status_code == 200, r.text
    nuevas = r.json()["contrasenas"]
    assert nuevas["dueno"] != datos["contrasenas"]["dueno"]
    r = await app_client.post(
        "/api/v1/auth/login", json={"identifier": dueno["email"], "password": datos["contrasenas"]["dueno"]}
    )
    assert r.status_code == 401
    r = await app_client.post("/api/v1/auth/login", json={"identifier": dueno["email"], "password": nuevas["dueno"]})
    assert r.status_code == 200, r.text


async def test_datos_de_prueba_solo_en_test(app_client, tenant_setup, testers_on) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    from core.config import get_settings

    testers_on.setattr(get_settings(), "qa_testers_enabled", False)
    r = await app_client.get("/api/v1/admin/qa/datos", headers=master)
    assert r.status_code == 200 and r.json()["disponible"] is False
    r = await app_client.post("/api/v1/admin/qa/datos", headers=master)
    assert r.status_code == 409
    r = await app_client.post("/api/v1/admin/qa/datos/contrasenas", headers=master)
    assert r.status_code == 409


async def test_un_colaborador_de_negocio_no_prepara_datos(app_client, tenant_setup, testers_on) -> None:
    t1, _t2 = tenant_setup
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"owner-datos-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador", active=True)
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=t1, admin_id=admin.id, role="owner"))
    headers = await api_headers(app_client, t1, email=email, password=PASSWORD)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 403
    r = await app_client.get("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 403


async def test_revocar_al_tester_apaga_sus_cuentas_y_sin_la_variable_no_entran(app_client, tenant_setup, testers_on) -> None:
    from sqlalchemy import select

    from core.config import get_settings
    from core.db.models import Admin
    from core.db.session import get_session
    from core.qa import testers

    t1, _t2 = tenant_setup
    headers, email = await _tester(app_client, t1)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    datos = r.json()
    dueno = next(c for c in datos["cuentas"] if c["rol"] == "dueno")
    login = {"identifier": dueno["email"], "password": datos["contrasenas"]["dueno"]}

    # Sin QA_TESTERS_ENABLED las cuentas de prueba tampoco entran.
    testers_on.setattr(get_settings(), "qa_testers_enabled", False)
    r = await app_client.post("/api/v1/auth/login", json=login)
    assert r.status_code == 401
    testers_on.setattr(get_settings(), "qa_testers_enabled", True)
    r = await app_client.post("/api/v1/auth/login", json=login)
    assert r.status_code == 200, r.text

    async with get_session() as session:
        tester_id = (await session.execute(select(Admin.id).where(Admin.email == email))).scalar_one()
    await testers.revocar_tester(tester_id)
    async with get_session() as session:
        activas = (
            await session.execute(
                select(Admin.active).where(Admin.email.in_([c["email"] for c in datos["cuentas"]]))
            )
        ).scalars().all()
    assert activas == [False, False, False]
    r = await app_client.post("/api/v1/auth/login", json=login)
    assert r.status_code == 401


async def _borradores_y_versiones(tid: uuid.UUID) -> dict[str, tuple[dict, dict, int]]:
    """Por key de especialista: (borrador, última versión, lock_version del borrador)."""
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (
                await session.execute(
                    text(
                        "SELECT a.key, d.content, d.lock_version, "
                        "(SELECT v.content FROM agent_versions v WHERE v.agent_id = a.id ORDER BY v.version DESC LIMIT 1) "
                        "FROM agents a JOIN agent_drafts d ON d.agent_id = a.id AND d.deleted_at IS NULL "
                        "WHERE a.tenant_id = :t AND a.kind = 'specialist' AND a.deleted_at IS NULL"
                    ),
                    {"t": tid},
                )
            ).all()
    return {f[0]: (f[1], f[3], f[2]) for f in filas}


async def test_borradores_iguales_a_la_version_descripcion_y_reparacion(app_client, tenant_setup, testers_on) -> None:
    """Los agentes del negocio de prueba dejan el borrador igual a la v1 (no el inicial vacío),
    Personalizar › Datos del cliente ve sus operaciones y el negocio trae descripción. Un negocio
    sembrado antes del arreglo (borrador inicial, descripción vieja) se repara al volver a preparar."""
    from sqlalchemy import select, update

    from core.agents.versiones import contenido_inicial
    from core.db.models import AgentDraftRecord, AgentRecord, AppSettings
    from core.db.session import get_session
    from core.qa.datos import DESCRIPCION_QA, DESCRIPCION_QA_ANTERIOR
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    headers, _email = await _tester(app_client, t1)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    datos = r.json()
    assert datos["agent_os"]["estado"] == "listo", datos["agent_os"]
    tid = uuid.UUID(datos["negocio"]["id"])

    agentes = await _borradores_y_versiones(tid)
    assert {"ventas", "agenda", "cobranza"} <= set(agentes)
    for key in ("ventas", "agenda", "cobranza"):
        borrador, version, _lock = agentes[key]
        assert borrador == version and (borrador.get("tools") or borrador.get("procedures")), key

    async with tenant_scope(tid):
        async with get_session() as session:
            descripcion = (
                await session.execute(select(AppSettings.business_description).where(AppSettings.tenant_id == tid))
            ).scalar_one()
            ventas_id = (
                await session.execute(select(AgentRecord.id).where(AgentRecord.tenant_id == tid, AgentRecord.key == "ventas"))
            ).scalar_one()
    assert descripcion == DESCRIPCION_QA

    dueno = next(c for c in datos["cuentas"] if c["rol"] == "dueno")
    h_dueno = await api_headers(app_client, tid, email=dueno["email"], password=datos["contrasenas"]["dueno"])
    r = await app_client.get(f"/api/v1/admin/agents/{ventas_id}/datos-cliente", headers=h_dueno)
    assert r.status_code == 200, r.text
    assert r.json()["acciones"], r.json()

    # Negocio sembrado antes del arreglo: borrador inicial intacto y la descripción vieja.
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(AgentDraftRecord)
                .where(AgentDraftRecord.tenant_id == tid, AgentDraftRecord.agent_id == ventas_id)
                .values(content=contenido_inicial("specialist"), lock_version=0)
            )
            await session.execute(
                update(AppSettings).where(AppSettings.tenant_id == tid).values(business_description=DESCRIPCION_QA_ANTERIOR)
            )
    # Aun así, Datos del cliente lee la versión publicada (el borrador intacto no es lo que ejecuta).
    r = await app_client.get(f"/api/v1/admin/agents/{ventas_id}/datos-cliente", headers=h_dueno)
    assert r.status_code == 200 and r.json()["acciones"], r.text

    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200, r.text
    borrador, version, _lock = (await _borradores_y_versiones(tid))["ventas"]
    assert borrador == version
    async with tenant_scope(tid):
        async with get_session() as session:
            descripcion = (
                await session.execute(select(AppSettings.business_description).where(AppSettings.tenant_id == tid))
            ).scalar_one()
    assert descripcion == DESCRIPCION_QA


async def test_cuentas_huerfanas_se_reutilizan_y_una_ajena_no_deja_negocio(app_client, tenant_setup, testers_on) -> None:
    """Si se borró la fila de `qa_tester_datasets` y las cuentas quedaron, «Preparar» las adopta
    (mismo id). Una cuenta con el correo de prueba vinculada a otro negocio da 409 y no deja un
    negocio a medias."""
    from sqlalchemy import delete, func, select

    from core.admin.auth import hash_password
    from core.db.models import Admin, QaTesterDataset, Tenant, TenantUser
    from core.db.session import get_session
    from core.qa.datos import correo_de, slug_de

    t1, _t2 = tenant_setup
    headers, email = await _tester(app_client, t1)
    async with get_session() as session:
        tester_id = (await session.execute(select(Admin.id).where(Admin.email == email))).scalar_one()

    # Cuentas huérfanas: existen con el correo de prueba, sin vínculos ni fila.
    huerfanas: dict[str, uuid.UUID] = {}
    async with get_session() as session:
        for rol in ("dueno", "gestor", "colaborador"):
            cuenta = Admin(
                email=correo_de(rol, tester_id), password_hash=hash_password("vieja-123456"), role="colaborador", active=True
            )
            session.add(cuenta)
            await session.flush()
            huerfanas[rol] = cuenta.id
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    async with get_session() as session:
        ds = (await session.execute(select(QaTesterDataset).where(QaTesterDataset.admin_id == tester_id))).scalar_one()
    assert (ds.dueno_admin_id, ds.gestor_admin_id, ds.colaborador_admin_id) == (
        huerfanas["dueno"], huerfanas["gestor"], huerfanas["colaborador"]
    )
    dueno = next(c for c in r.json()["cuentas"] if c["rol"] == "dueno")
    login = await app_client.post(
        "/api/v1/auth/login", json={"identifier": dueno["email"], "password": r.json()["contrasenas"]["dueno"]}
    )
    assert login.status_code == 200, login.text

    # Otro tester cuyo correo de prueba ya está vinculado a un negocio real: 409, sin negocio a medias.
    headers2, email2 = await _tester(app_client, t1)
    async with get_session() as session:
        tester2 = (await session.execute(select(Admin.id).where(Admin.email == email2))).scalar_one()
        ajena = Admin(email=correo_de("gestor", tester2), password_hash=hash_password("x-123456789"), role="colaborador", active=True)
        session.add(ajena)
        await session.flush()
        session.add(TenantUser(tenant_id=t1, admin_id=ajena.id, role="colaborador"))
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers2)
    assert r.status_code == 409, r.text
    assert "ya pertenece a otra cuenta" in r.text
    async with get_session() as session:
        negocios = (await session.execute(select(func.count()).select_from(Tenant).where(Tenant.slug == slug_de(tester2)))).scalar_one()
        await session.execute(delete(TenantUser).where(TenantUser.admin_id == ajena.id))
    assert negocios == 0

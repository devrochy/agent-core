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
        json={"usuario": gestor_cuenta["email"], "password": datos["contrasenas"]["gestor"]},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("accessToken"), r.json()

    # Idempotente: un segundo «Preparar» no crea otro negocio.
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 409

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

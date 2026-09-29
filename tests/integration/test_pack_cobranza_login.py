"""Login unificado de la API de campo (ADR-0081).

Cubre: login sin slug con una membresía, con varias (`seleccionRequerida`), selección
explícita por slug, credenciales inválidas y acceso total del master. Requiere el
stack local (Postgres + Redis).
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PACK_AUTH = "/api/pack/pack_cobranza/auth"
PW = "password123"


async def _tenant_cobranza(nombre: str, slug: str, owner_email: str):
    from core.onboarding import onboard_tenant

    return await onboard_tenant(
        name=nombre,
        slug=slug,
        business_description="Tenant de prueba de login.",
        persona={"tone": "cercano"},
        admin_email=owner_email,
        admin_password=PW,
        packs=["pack_cobranza"],
    )


async def _gestor_compartido(email: str, username: str, tenants) -> None:
    """Crea un admin colaborador + perfil `Gestor` activo en cada tenant."""
    from pack_cobranza.models import Gestor

    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        admin = Admin(
            email=email,
            username=username,
            password_hash=hash_password(PW),
            role="colaborador",
        )
        session.add(admin)
        await session.flush()
        admin_id = admin.id

    for tid in tenants:
        async with tenant_scope(tid):
            async with get_session() as session:
                session.add(TenantUser(tenant_id=tid, admin_id=admin_id, role="colaborador"))
                session.add(Gestor(tenant_id=tid, admin_id=admin_id, nombre="Gestor Test"))


async def _admin_master(email: str, password: str) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        session.add(
            Admin(email=email, password_hash=hash_password(password), role="master")
        )


async def _login(app_client, usuario: str, password: str = PW, tenant: str | None = None):
    body = {"usuario": usuario, "password": password}
    if tenant is not None:
        body["tenant"] = tenant
    return await app_client.post(f"{PACK_AUTH}/login", json=body)


async def test_owner_login_sin_slug(app_client) -> None:
    slug = uniq("login-own")
    email = f"{slug}@test.local"
    await _tenant_cobranza("Login Own", slug, email)

    r = await _login(app_client, email)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["accessToken"] and data["refreshToken"]
    assert data["rol"] == "propietario"
    assert data["tenantSlug"] == slug
    assert data["propietario"]["usuario"] == email


async def test_gestor_en_dos_negocios_selecciona(app_client) -> None:
    base = uniq("login-multi")
    slug_a, slug_b = f"{base}-a", f"{base}-b"
    await _tenant_cobranza("Login Multi A", slug_a, f"{slug_a}@test.local")
    await _tenant_cobranza("Login Multi B", slug_b, f"{slug_b}@test.local")

    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        ta = (await session.execute(select(Tenant.id).where(Tenant.slug == slug_a))).scalar_one()
        tb = (await session.execute(select(Tenant.id).where(Tenant.slug == slug_b))).scalar_one()

    email = f"gestor-{base}@test.local"
    await _gestor_compartido(email, f"g-{base}", (ta, tb))

    # Sin slug: pide selección y lista los dos negocios con rol gestor.
    r = await _login(app_client, email)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seleccionRequerida"] is True
    slugs = sorted(n["slug"] for n in data["negocios"])
    assert slugs == sorted([slug_a, slug_b])
    assert all(n["rol"] == "gestor" for n in data["negocios"])

    # Con slug: emite tokens del negocio elegido.
    r2 = await _login(app_client, email, tenant=slug_a)
    assert r2.status_code == 200, r2.text
    sesion = r2.json()
    assert sesion["rol"] == "gestor"
    assert sesion["tenantSlug"] == slug_a
    assert sesion["gestor"]["id"]


async def test_credenciales_invalidas(app_client) -> None:
    r = await _login(app_client, "nadie@test.local", "mala")
    assert r.status_code == 401


async def test_tenant_sin_acceso(app_client) -> None:
    slug = uniq("login-own2")
    email = f"{slug}@test.local"
    await _tenant_cobranza("Login Own 2", slug, email)
    r = await _login(app_client, email, tenant="tenant-que-no-existe")
    assert r.status_code == 401


async def test_master_ve_todos_los_negocios(app_client) -> None:
    slug = uniq("login-master")
    await _tenant_cobranza("Login Master", slug, f"{slug}@test.local")

    email = f"master-{slug}@test.local"
    await _admin_master(email, PW)

    r = await _login(app_client, email)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seleccionRequerida"] is True
    por_slug = {n["slug"]: n for n in data["negocios"]}
    assert slug in por_slug
    assert por_slug[slug]["rol"] == "master"

    r2 = await _login(app_client, email, tenant=slug)
    assert r2.status_code == 200, r2.text
    assert r2.json()["rol"] == "master"


async def test_acceso_total_owner_y_gestor(app_client) -> None:
    """El owner ve todas las carteras; el gestor solo las suyas (ADR-0081)."""
    from pack_cobranza.models import Cartera, Gestor
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("login-full")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Login Full", slug, owner_email)

    g_email = f"gestor-{slug}@test.local"
    await _gestor_compartido(g_email, f"g-{slug}", (tid,))

    async with tenant_scope(tid):
        async with get_session() as session:
            gestor = (
                await session.execute(select(Gestor).where(Gestor.tenant_id == tid))
            ).scalar_one()
            session.add(Cartera(tenant_id=tid, nombre="Cartera 1", gestor_id=gestor.id))

    mis_carteras = "/api/pack/pack_cobranza/gestor/mis-carteras"

    owner = (await _login(app_client, owner_email)).json()
    r = await app_client.get(
        mis_carteras, headers={"Authorization": f"Bearer {owner['accessToken']}"}
    )
    assert r.status_code == 200, r.text
    assert [c["nombre"] for c in r.json()] == ["Cartera 1"]

    gestor_sesion = (await _login(app_client, g_email, tenant=slug)).json()
    r2 = await app_client.get(
        mis_carteras, headers={"Authorization": f"Bearer {gestor_sesion['accessToken']}"}
    )
    assert r2.status_code == 200, r2.text
    assert [c["nombre"] for c in r2.json()] == ["Cartera 1"]

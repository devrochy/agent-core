"""RBAC del panel (ADR-0047): permisos de sección, alcance global y default-deny.

Verifica el gate central del middleware y la resolución de permisos:
- master: bypass total.
- admin con permiso global `read` (marketing): lee las secciones concedidas,
  no escribe y no entra a secciones no concedidas, sobre cualquier tenant.
- admin sin permisos: 403.
- owner de tenant: hereda `write`; colaborador de tenant: `read`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}@example.test"


async def _login(app_client, email: str, password: str = PASSWORD) -> str:
    r = await app_client.post("/admin/login", data={"email": email, "password": password})
    assert r.status_code == 303, f"login falló para {email}: {r.text}"
    return r.headers["set-cookie"].split(";")[0]


async def _make_admin(email: str, role: str = "colaborador") -> uuid.UUID:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role=role)
        session.add(admin)
        await session.flush()
        return admin.id


async def _grant(admin_id: uuid.UUID, section: str, access: str, tenant_id: uuid.UUID | None = None) -> None:
    from core.db.models import AdminPermission
    from core.db.session import get_session

    async with get_session() as session:
        session.add(
            AdminPermission(admin_id=admin_id, section=section, access=access, tenant_id=tenant_id)
        )


async def _link(admin_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))


async def test_master_entra_a_admins(app_client) -> None:
    cookie = await _login(app_client, "admin@local", "admin123")
    r = await app_client.get("/admin/admins", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Administradores" in r.text


async def test_master_crea_admin_y_concede_permiso(app_client, moto_setup) -> None:
    cookie = await _login(app_client, "admin@local", "admin123")
    email = _uniq("nuevo-admin")

    r = await app_client.post(
        "/admin/admins",
        headers={"Cookie": cookie},
        data={"email": email, "password": PASSWORD, "role": "colaborador"},
    )
    assert r.status_code == 303

    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one()

    r = await app_client.post(
        f"/admin/admins/{admin.id}/grant",
        headers={"Cookie": cookie},
        data={"section": "costs", "access": "read", "tenant": ""},
    )
    assert r.status_code == 303

    from core.db.models import AdminPermission

    async with get_session() as session:
        perms = (
            await session.execute(
                select(AdminPermission).where(AdminPermission.admin_id == admin.id)
            )
        ).scalars().all()
    assert [(p.section, p.access, p.tenant_id) for p in perms] == [("costs", "read", None)]


async def test_marketing_global_solo_lectura(app_client, moto_setup) -> None:
    """Permiso global `read` sobre costs/conversations: lee en cualquier tenant,
    no escribe, y no entra a secciones no concedidas."""
    email = _uniq("marketing")
    admin_id = await _make_admin(email)
    await _grant(admin_id, "costs", "read")
    await _grant(admin_id, "conversations", "read")

    cookie = await _login(app_client, email)
    headers = {"Cookie": cookie}

    assert (await app_client.get("/admin/costos", headers=headers)).status_code == 200
    assert (await app_client.get("/admin/conversaciones", headers=headers)).status_code == 200
    # Secciones no concedidas (aunque sea tenant-scoped) → 403.
    assert (await app_client.get("/admin/configuracion", headers=headers)).status_code == 403
    assert (await app_client.get("/admin/tenants", headers=headers)).status_code == 403
    # Solo lectura: un POST sobre una sección no concedida → 403.
    assert (
        await app_client.post("/admin/configuracion/toggle-bot", headers=headers)
    ).status_code == 403


async def test_lectura_no_permite_escritura_en_la_misma_seccion(app_client, moto_setup) -> None:
    email = _uniq("solo-lectura")
    admin_id = await _make_admin(email)
    await _grant(admin_id, "costs", "read")

    cookie = await _login(app_client, email)
    headers = {"Cookie": cookie}

    assert (await app_client.get("/admin/costos", headers=headers)).status_code == 200
    # `sync` es POST sobre costs → exige write.
    assert (await app_client.post("/admin/costos/meta/sync", headers=headers)).status_code == 403


async def test_admin_sin_permisos_no_accede(app_client) -> None:
    email = _uniq("sin-permisos")
    await _make_admin(email)
    cookie = await _login(app_client, email)
    assert (await app_client.get("/admin", headers={"Cookie": cookie})).status_code == 403


async def test_owner_hereda_write_y_colaborador_read(app_client, tenant_setup) -> None:
    t1, _t2 = tenant_setup

    owner_email = _uniq("owner")
    owner_id = await _make_admin(owner_email)
    await _link(owner_id, t1, "owner")

    colab_email = _uniq("colab")
    colab_id = await _make_admin(colab_email)
    await _link(colab_id, t1, "colaborador")

    owner_cookie = await _login(app_client, owner_email)
    colab_cookie = await _login(app_client, colab_email)

    # Ambos ven la sección (read); solo el owner puede escribir.
    assert (
        await app_client.get("/admin/conversaciones", headers={"Cookie": owner_cookie})
    ).status_code == 200
    assert (
        await app_client.get("/admin/conversaciones", headers={"Cookie": colab_cookie})
    ).status_code == 200
    assert (
        await app_client.post("/admin/configuracion/toggle-bot", headers={"Cookie": colab_cookie})
    ).status_code == 403


async def test_gate_de_pack_por_seccion(app_client, moto_setup) -> None:
    """La UI de un pack (`/crm`) exige su sección: el owner del tenant la hereda;
    un admin con permisos solo en otras secciones no."""
    _t1, moto = moto_setup

    owner_email = _uniq("owner-pack")
    owner_id = await _make_admin(owner_email)
    await _link(owner_id, moto, "owner")
    owner_cookie = await _login(app_client, owner_email)
    assert (await app_client.get("/crm/", headers={"Cookie": owner_cookie})).status_code == 200

    mkt_email = _uniq("mkt-sin-pack")
    mkt_id = await _make_admin(mkt_email)
    await _grant(mkt_id, "costs", "read")
    mkt_cookie = await _login(app_client, mkt_email)
    assert (await app_client.get("/crm/", headers={"Cookie": mkt_cookie})).status_code == 403


async def test_no_master_no_gestiona_admins(app_client) -> None:
    """Un no-master con `admins:write` no puede entrar a la gestión ni crear un
    master (auto-escalada): la administración de identidades es solo de master."""
    email = _uniq("gestor")
    admin_id = await _make_admin(email)
    await _grant(admin_id, "admins", "write")
    cookie = await _login(app_client, email)
    headers = {"Cookie": cookie}

    assert (await app_client.get("/admin/admins", headers=headers)).status_code == 303

    target = _uniq("escalada")
    r = await app_client.post(
        "/admin/admins",
        headers=headers,
        data={"email": target, "password": PASSWORD, "role": "master"},
    )
    assert r.status_code == 303

    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        created = (
            await session.execute(select(Admin).where(Admin.email == target))
        ).scalar_one_or_none()
    assert created is None

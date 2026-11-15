"""Baja lógica de cuentas de equipo (ADR-0198).

Verifica que eliminar una cuenta:
- corta el acceso y la saca de Equipo, sin borrar la fila `admins` ni cambiar su correo;
- conserva el historial de cartera (`Pago.registrado_por`) y da de baja lógica el `Gestor`
  sin borrar sus registros ni sus carteras;
- respeta la jerarquía (dueño solo colaboradores exclusivos; master no deja un negocio sin
  dueño ni toca cuentas de plataforma);
- solo se revierte con la acción explícita de reactivación, que revive la MISMA identidad.

Requiere el stack local (`AGENT_CORE_INTEGRATION=1`).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.aislamiento,
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


async def _link(admin_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))


async def _gestor_y_pago(tenant_id: uuid.UUID, admin_id: uuid.UUID, email: str) -> None:
    """Perfil de Cobranza + un pago con el correo real del gestor en el historial."""
    from pack_cobranza.models import Gestor, Pago

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id), get_session() as session:
        session.add(
            Gestor(tenant_id=tenant_id, admin_id=admin_id, nombre="Gestor de prueba", correo=email)
        )
        session.add(
            Pago(
                tenant_id=tenant_id,
                cliente_id=uuid.uuid4(),
                valor=100,
                metodo_pago="efectivo",
                registrado_por=email,
            )
        )


async def _limpiar(app_client, master_cookie: str, *admin_ids: uuid.UUID) -> None:
    """Deja las cuentas de la prueba en baja: no deben ocupar cupo de otras pruebas."""
    for admin_id in admin_ids:
        await app_client.post(
            f"/api/v1/admin/collaborators/{admin_id}/delete",
            headers={"Cookie": master_cookie},
        )


async def _estado(admin_id: uuid.UUID, tenant_id: uuid.UUID, email: str):
    from pack_cobranza.models import Gestor, Pago

    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        links = (
            await session.execute(
                select(func.count()).select_from(TenantUser).where(TenantUser.admin_id == admin_id)
            )
        ).scalar_one()
    async with tenant_scope(tenant_id), get_session() as session:
        gestor = (
            await session.execute(select(Gestor).where(Gestor.admin_id == admin_id))
        ).scalar_one_or_none()
        pago = (
            await session.execute(select(Pago).where(Pago.registrado_por == email))
        ).scalar_one_or_none()
    return admin, int(links), gestor, pago


async def test_master_elimina_y_conserva_historial(app_client, tenant_setup) -> None:
    t1, _t2 = tenant_setup
    master = await _login(app_client, "admin@local", "admin123")
    email = _uniq("baja-gestor")
    target_id = await _make_admin(email)
    await _link(target_id, t1, "colaborador")
    await _gestor_y_pago(t1, target_id, email)

    r = await app_client.post(
        f"/api/v1/admin/collaborators/{target_id}/delete",
        headers={"Cookie": master},
    )
    assert r.status_code == 204, r.text

    admin, links, gestor, pago = await _estado(target_id, t1, email)
    assert admin is not None
    assert admin.deleted_at is not None
    assert admin.deleted_by is not None
    assert admin.active is False
    # La identidad NO se anonimiza: el historial de cartera guarda este correo como texto.
    assert admin.email == email
    assert links == 0
    # El Gestor se da de baja lógicamente, sin borrar sus carteras ni su historial.
    assert gestor is not None
    assert gestor.deleted_at is not None
    assert gestor.estatus == "baja"
    assert gestor.admin_id == target_id
    assert pago is not None and pago.registrado_por == email

    # Ya no entra.
    assert (
        await app_client.post("/admin/login", data={"email": email, "password": PASSWORD})
    ).status_code == 401

    # Sale del listado vivo; aparece en el de eliminadas con opción de reactivar.
    vivos = await app_client.get(
        "/api/v1/admin/collaborators?todos=1", headers={"Cookie": master}
    )
    assert vivos.status_code == 200
    assert str(target_id) not in str(vivos.json())

    eliminadas = await app_client.get(
        "/api/v1/admin/collaborators?eliminadas=1", headers={"Cookie": master}
    )
    assert eliminadas.status_code == 200
    fila = next((f for f in eliminadas.json() if f["id"] == str(target_id)), None)
    assert fila is not None
    assert fila["deleted"] is True
    assert fila["can_reactivate"] is True
    assert fila["can_delete"] is False

    # La reactivación explícita revive la MISMA identidad y el mismo Gestor.
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{target_id}/reactivate",
        headers={"Cookie": master},
    )
    assert r.status_code == 204, r.text
    admin, links, gestor, pago = await _estado(target_id, t1, email)
    assert admin is not None and admin.deleted_at is None and admin.active is True
    assert links == 1  # vuelve al mismo negocio con su vínculo
    assert gestor is not None and gestor.deleted_at is None and gestor.estatus == "activo"
    assert pago is not None and pago.registrado_por == email
    assert (
        await app_client.post("/admin/login", data={"email": email, "password": PASSWORD})
    ).status_code == 303

    # La prueba no debe dejar una cuenta viva ocupando cupo de otras pruebas.
    await _limpiar(app_client, master, target_id)


async def test_owner_solo_elimina_colaborador_exclusivo(app_client, tenant_setup) -> None:
    t1, t2 = tenant_setup
    owner_email = _uniq("owner-baja")
    owner_id = await _make_admin(owner_email)
    await _link(owner_id, t1, "owner")
    cookie = await _login(app_client, owner_email)
    headers = {"Cookie": cookie, "X-Tenant-Id": str(t1)}

    exclusivo = await _make_admin(_uniq("colab-exclusivo"))
    await _link(exclusivo, t1, "colaborador")
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{exclusivo}/delete", headers=headers
    )
    assert r.status_code == 204, r.text

    compartida = await _make_admin(_uniq("colab-compartida"))
    await _link(compartida, t1, "colaborador")
    await _link(compartida, t2, "colaborador")
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{compartida}/delete", headers=headers
    )
    assert r.status_code == 400

    otro_owner = await _make_admin(_uniq("owner-par"))
    await _link(otro_owner, t1, "owner")
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{otro_owner}/delete", headers=headers
    )
    assert r.status_code == 400

    r = await app_client.post(
        f"/api/v1/admin/collaborators/{owner_id}/delete", headers=headers
    )
    assert r.status_code == 400

    master = await _login(app_client, "admin@local", "admin123")
    await _limpiar(app_client, master, compartida)


async def test_master_no_deja_un_negocio_sin_dueno(app_client) -> None:
    from core.db.models import Tenant
    from core.db.session import get_session

    master = await _login(app_client, "admin@local", "admin123")
    async with get_session() as session:
        tenant = Tenant(
            name="Baja con dueño único",
            slug=f"baja-dueno-{uuid.uuid4().hex[:6]}",
            status="active",
        )
        session.add(tenant)
        await session.flush()
        tid = tenant.id

    unico = await _make_admin(_uniq("owner-unico"))
    await _link(unico, tid, "owner")

    r = await app_client.post(
        f"/api/v1/admin/collaborators/{unico}/delete", headers={"Cookie": master}
    )
    assert r.status_code == 400
    assert "sin propietario" in r.text

    reemplazo = await _make_admin(_uniq("owner-reemplazo"))
    await _link(reemplazo, tid, "owner")
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{unico}/delete", headers={"Cookie": master}
    )
    assert r.status_code == 204, r.text
    await _limpiar(app_client, master, reemplazo)


async def test_no_se_reactiva_con_toggle_y_el_alta_no_reasigna(app_client, tenant_setup) -> None:
    t1, _t2 = tenant_setup
    master = await _login(app_client, "admin@local", "admin123")
    email = _uniq("baja-toggle")
    target_id = await _make_admin(email)
    await _link(target_id, t1, "colaborador")

    r = await app_client.post(
        f"/api/v1/admin/collaborators/{target_id}/delete", headers={"Cookie": master}
    )
    assert r.status_code == 204

    # Desactivar/activar no resucita una eliminada.
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{target_id}/toggle", headers={"Cookie": master}
    )
    assert r.status_code == 400
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = await session.get(Admin, target_id)
    assert admin is not None and admin.active is False

    # El alta con el mismo correo no crea otra identidad: avisa que hay que reactivarla.
    r = await app_client.post(
        "/api/v1/admin/collaborators",
        headers={"Cookie": master},
        json={"email": email, "password": PASSWORD, "role": "colaborador"},
    )
    assert r.status_code == 400
    assert "eliminada" in r.text
    async with get_session() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(Admin).where(Admin.email == email)
            )
        ).scalar_one()
    assert total == 1

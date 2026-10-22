"""Sección Equipo (D-SPEC-34): listado por negocio, alta en dos pasos y perfil.

Con la BD real: `GET /admin/collaborators` devuelve solo el negocio activo (el master lo ve todo
con `?todos=1`); `GET /existe` y `GET /username-disponible` alimentan el alta en dos pasos; el
alta guarda `nombre` y `telefono` y no elige negocio (cae en el activo).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "clave-segura-123"
COLABS = "/api/v1/admin/collaborators"


async def _negocio(sufijo: str, owner_email: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Equipo {sufijo}",
        slug=f"equipo-{sufijo}",
        business_description="Negocio de prueba de la sección Equipo.",
        persona={"tone": "cercano"},
        admin_email=owner_email,
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    return tid


async def _master(email: str) -> uuid.UUID:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="master")
        session.add(admin)
        await session.flush()
        return admin.id


async def _headers(app_client, email: str, tenant_id: uuid.UUID | None = None) -> dict[str, str]:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PW})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    if tenant_id is not None:
        h["X-Tenant-Id"] = str(tenant_id)
    return h


async def _admin_id(app_client, email: str) -> uuid.UUID:
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one()
        return admin.id


async def test_existe_y_username_disponible(app_client) -> None:
    s = uniq("lookup")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    h = await _headers(app_client, owner, tid)

    existe = (await app_client.get(f"{COLABS}/existe", params={"email": owner}, headers=h)).json()
    assert existe == {"existe": True}
    libre = (await app_client.get(f"{COLABS}/existe", params={"email": f"nuevo-{s}@p0.test"}, headers=h)).json()
    assert libre == {"existe": False}

    disp = (await app_client.get(f"{COLABS}/username-disponible", params={"username": f"u-{s}"}, headers=h)).json()
    assert disp == {"disponible": True}


async def test_listado_acotado_al_negocio_y_todos_para_el_master(app_client) -> None:
    s = uniq("lista")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    master_email = f"master-{s}@p0.test"
    await _master(master_email)

    # El dueño ve solo su negocio (él mismo).
    h_owner = await _headers(app_client, owner, tid)
    lista = (await app_client.get(COLABS, headers=h_owner)).json()
    assert {c["email"] for c in lista} == {owner}

    # El master con el negocio activo ve solo ese negocio; con ?todos=1 ve la plataforma.
    h_master = await _headers(app_client, master_email, tid)
    acotada = (await app_client.get(COLABS, headers=h_master)).json()
    assert {c["email"] for c in acotada} == {owner}
    toda = (await app_client.get(COLABS, params={"todos": "1"}, headers=h_master)).json()
    assert master_email in {c["email"] for c in toda}


async def test_alta_guarda_nombre_y_telefono_en_el_negocio_activo(app_client) -> None:
    s = uniq("alta")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    h = await _headers(app_client, owner, tid)

    email = f"ana-{s}@p0.test"
    r = await app_client.post(
        COLABS,
        headers=h,
        json={
            "email": email,
            "password": "otra-clave-456",
            "role": "colaborador",
            "username": f"ana-{s}",
            "nombre": "Ana Pérez",
            "telefono": "+57 300 123 4567",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["resultado"] == "creado"

    admin_id = await _admin_id(app_client, email)
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        assert admin.nombre == "Ana Pérez"
        assert admin.telefono == "+57 300 123 4567"
        assert admin.username == f"ana-{s}"

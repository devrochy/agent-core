"""Invitaciones de membresía multi-negocio (ADR-0081).

Flujo completo: un owner invita a una identidad existente → el invitado (sin
membresías) recibe el token de membresía en el login → lista/acepta la invitación
desde la API de campo → el negocio queda habilitado. Requiere el stack local.
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

PACK = "/api/pack/pack_cobranza"
PACK_AUTH = f"{PACK}/auth"
PW = "password123"


async def _tenant_cobranza(nombre: str, slug: str, owner_email: str):
    from core.onboarding import onboard_tenant

    return await onboard_tenant(
        name=nombre,
        slug=slug,
        business_description="Tenant de prueba de invitaciones.",
        persona={"tone": "cercano"},
        admin_email=owner_email,
        admin_password=PW,
        packs=["pack_cobranza"],
    )


async def _admin_colaborador(email: str) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        session.add(
            Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        )


async def _owner_headers(app_client, email: str, tenant_id) -> dict[str, str]:
    r = await app_client.post(
        "/api/v1/auth/login", json={"identifier": email, "password": PW}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "X-Tenant-Id": str(tenant_id)}


async def test_flujo_invitacion_aceptada(app_client) -> None:
    slug = uniq("inv")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Inv", slug, owner_email)

    invitee = f"inv-{slug}@test.local"
    await _admin_colaborador(invitee)

    headers = await _owner_headers(app_client, owner_email, tid)
    r = await app_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"email": invitee, "role": "colaborador", "pack_roles": ["pack_cobranza:gestor"]},
    )
    assert r.status_code == 204, r.text

    # El invitado no tiene membresías: el login unificado devuelve token de membresía.
    rl = await app_client.post(f"{PACK_AUTH}/login", json={"usuario": invitee, "password": PW})
    assert rl.status_code == 200, rl.text
    data = rl.json()
    assert data.get("requiereMembresia") is True
    assert len(data["invitaciones"]) == 1
    assert data["invitaciones"][0]["slug"] == slug
    mtok = {"Authorization": f"Bearer {data['membresiaToken']}"}

    # Lista y acepta con el token de membresía.
    r2 = await app_client.get(f"{PACK_AUTH}/invitaciones", headers=mtok)
    assert r2.status_code == 200, r2.text
    invitacion = r2.json()[0]
    r3 = await app_client.post(
        f"{PACK_AUTH}/invitaciones/{invitacion['id']}/aceptar", headers=mtok
    )
    assert r3.status_code == 200, r3.text

    # Ya es miembro: el login unificado emite tokens del negocio (rol gestor).
    r4 = await app_client.post(f"{PACK_AUTH}/login", json={"usuario": invitee, "password": PW})
    assert r4.status_code == 200, r4.text
    sesion = r4.json()
    assert sesion["tenantSlug"] == slug
    assert sesion["rol"] == "gestor"

    # La invitación ya no está pendiente.
    r5 = await app_client.get(f"{PACK_AUTH}/invitaciones", headers=mtok)
    assert r5.status_code == 200
    assert r5.json() == []


async def test_no_se_puede_invitar_ya_miembro(app_client) -> None:
    slug = uniq("inv-dup")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Inv Dup", slug, owner_email)
    headers = await _owner_headers(app_client, owner_email, tid)

    r = await app_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"email": owner_email, "role": "colaborador"},
    )
    assert r.status_code == 400
    assert "pertenece" in r.json()["error"]["message"]


async def test_no_se_puede_invitar_master(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    slug = uniq("inv-master")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Inv Master", slug, owner_email)

    master_email = f"m-{slug}@test.local"
    async with get_session() as session:
        session.add(Admin(email=master_email, password_hash=hash_password(PW), role="master"))

    headers = await _owner_headers(app_client, owner_email, tid)
    r = await app_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"email": master_email, "role": "colaborador"},
    )
    assert r.status_code == 400
    assert "master" in r.json()["error"]["message"]


async def test_invitar_identidad_inexistente(app_client) -> None:
    slug = uniq("inv-nuevo")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Inv Nuevo", slug, owner_email)
    headers = await _owner_headers(app_client, owner_email, tid)
    r = await app_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"email": f"no-existe-{slug}@test.local", "role": "colaborador"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "identity_not_found"


async def test_create_collaborator_existente_invita(app_client) -> None:
    """Alta de colaborador con identidad existente → invitación, no duplicado (F3)."""
    slug = uniq("inv-colab")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Inv Colab", slug, owner_email)

    invitee = f"colab-{slug}@test.local"
    await _admin_colaborador(invitee)

    headers = await _owner_headers(app_client, owner_email, tid)
    r = await app_client.post(
        "/api/v1/admin/collaborators",
        headers=headers,
        json={"email": invitee, "password": PW, "role": "colaborador"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["resultado"] == "invitado"

    rl = await app_client.post(f"{PACK_AUTH}/login", json={"usuario": invitee, "password": PW})
    assert rl.status_code == 200, rl.text
    assert any(i["slug"] == slug for i in rl.json()["invitaciones"])


async def test_create_collector_existente_invita(app_client) -> None:
    """Alta de gestor con identidad existente → invitación (F3)."""
    slug = uniq("inv-gest")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Inv Gest", slug, owner_email)

    invitee = f"gest-{slug}@test.local"
    await _admin_colaborador(invitee)

    headers = await _owner_headers(app_client, owner_email, tid)
    r = await app_client.post(
        "/api/v1/pack/cobranza/collectors",
        headers=headers,
        json={"nombre": "Gestor Invitado", "correo": invitee, "password": PW},
    )
    assert r.status_code == 201, r.text
    assert r.json()["resultado"] == "invitado"

    rl = await app_client.post(f"{PACK_AUTH}/login", json={"usuario": invitee, "password": PW})
    assert rl.status_code == 200, rl.text
    assert any(i["slug"] == slug for i in rl.json()["invitaciones"])


async def test_create_collector_nuevo_crea(app_client) -> None:
    """Alta de gestor con identidad nueva → se crea y vincula directo (F3)."""
    slug = uniq("inv-gest-new")
    owner_email = f"{slug}@test.local"
    tid, _ = await _tenant_cobranza("Inv Gest New", slug, owner_email)

    headers = await _owner_headers(app_client, owner_email, tid)
    r = await app_client.post(
        "/api/v1/pack/cobranza/collectors",
        headers=headers,
        json={
            "nombre": "Gestor Nuevo",
            "correo": f"nuevo-{slug}@test.local",
            "password": PW,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["resultado"] == "creado"

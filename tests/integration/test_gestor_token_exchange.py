"""Sesión admin → tokens de campo (`POST /api/v1/pack/cobranza/gestor-token`, ADR-0098).

Cubre: intercambio del propietario (rol `propietario`), del gestor (rol
`gestor`, con `requiereVinculacion` sin dispositivo), negocio ajeno (403 del
principal), negocio sin pack de cobranza (403 del pack) y respuesta del
dispositivo no autorizado. Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"
EXCHANGE = "/api/v1/pack/cobranza/gestor-token"


async def _tenant(email: str, slug: str, packs: list[str]) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba del exchange.",
        persona={"tone": "cercano"},
        admin_email=email,
        admin_password=PW,
        packs=packs,
    )
    return tid


async def _login_admin(app_client, identifier: str) -> str:
    r = await app_client.post(
        "/api/v1/auth/login", json={"identifier": identifier, "password": PW}
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def _gestor_compartido(email: str, tenant_id: uuid.UUID) -> uuid.UUID:
    """Admin colaborador + perfil `Gestor` activo en el tenant (ADR-0066)."""
    from pack_cobranza.models import Gestor

    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        admin_id = admin.id
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role="colaborador"))
            session.add(Gestor(tenant_id=tenant_id, admin_id=admin_id, nombre="Gestor Ex"))
    return admin_id


def _headers(token: str, tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}


async def test_propietario_intercambia_tokens(app_client) -> None:
    slug = uniq("ex-own")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug, packs=["pack_cobranza"])
    token = await _login_admin(app_client, email)

    r = await app_client.post(EXCHANGE, json={}, headers=_headers(token, tid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rol"] == "propietario"
    assert body["tenantId"] == str(tid)
    assert body["accessToken"] and body["refreshToken"]
    assert body["dispositivoVinculado"] is False


async def test_gestor_intercambia_y_requiere_vinculacion(app_client) -> None:
    slug = uniq("ex-ges")
    owner_email = f"{slug}-owner@test.local"
    tid = await _tenant(owner_email, slug, packs=["pack_cobranza"])
    gestor_email = f"{slug}-gestor@test.local"
    await _gestor_compartido(gestor_email, tid)
    token = await _login_admin(app_client, gestor_email)

    r = await app_client.post(EXCHANGE, json={}, headers=_headers(token, tid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rol"] == "gestor"
    assert body["gestor"]["nombre"] == "Gestor Ex"
    assert body["requiereVinculacion"] is True
    assert body["estadoDispositivo"] == "none"
    assert len(body["permisos"]) == 14
    assert all(not p["habilitado"] for p in body["permisos"])


async def test_negocio_ajeno_es_403(app_client) -> None:
    slug_a = uniq("ex-a")
    slug_b = uniq("ex-b")
    email_a = f"{slug_a}@test.local"
    email_b = f"{slug_b}@test.local"
    await _tenant(email_a, slug_a, packs=["pack_cobranza"])
    tid_b = await _tenant(email_b, slug_b, packs=["pack_cobranza"])
    token_a = await _login_admin(app_client, email_a)

    r = await app_client.post(EXCHANGE, json={}, headers=_headers(token_a, tid_b))
    assert r.status_code == 403


async def test_sin_pack_de_cobranza_es_403(app_client) -> None:
    slug = uniq("ex-sin")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug, packs=[])
    token = await _login_admin(app_client, email)

    r = await app_client.post(EXCHANGE, json={}, headers=_headers(token, tid))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "sin_acceso_campo"


async def test_sin_sesion_es_401(app_client) -> None:
    r = await app_client.post(EXCHANGE, json={})
    assert r.status_code == 401

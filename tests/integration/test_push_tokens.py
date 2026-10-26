"""Registro de dispositivos de push (`/api/v1/me/push-token`, ADR-0099).

Cubre: alta idempotente, reactivación tras baja, baja por soft delete,
aislamiento RLS entre negocios y validación de payload. Requiere el stack
local (Postgres + Redis).
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

PW = "password123"


async def _tenant(email: str, slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de push.",
        admin_email=email,
        admin_password=PW,
        packs=[],
    )
    return tid


async def _login(app_client, email: str) -> str:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token: str, tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}


async def _tokens_activos(tid: uuid.UUID) -> int:
    from core.db.models import PushToken
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(
                    select(PushToken).where(
                        PushToken.tenant_id == tid, PushToken.deleted_at.is_(None)
                    )
                )
            ).scalars().all()


async def test_alta_idempotente_y_baja_reactivacion(app_client) -> None:
    slug = uniq("push")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug)
    token = await _login(app_client, email)
    h = _headers(token, tid)
    expo_token = f"ExponentPushToken[test-{uuid.uuid4().hex}]"

    r = await app_client.post("/api/v1/me/push-token", json={"token": expo_token}, headers=h)
    assert r.status_code == 204
    assert len(await _tokens_activos(tid)) == 1

    # Alta repetida del mismo token: idempotente, sigue habiendo uno.
    r = await app_client.post(
        "/api/v1/me/push-token", json={"token": expo_token, "platform": "ios"}, headers=h
    )
    assert r.status_code == 204
    filas = await _tokens_activos(tid)
    assert len(filas) == 1 and filas[0].platform == "ios"

    # Baja: soft delete, sin filas activas.
    r = await app_client.request("DELETE", "/api/v1/me/push-token", json={"token": expo_token}, headers=h)
    assert r.status_code == 204
    assert await _tokens_activos(tid) == []

    # Re-registro: reactiva el mismo token (sin duplicar por el índice parcial).
    r = await app_client.post("/api/v1/me/push-token", json={"token": expo_token}, headers=h)
    assert r.status_code == 204
    assert len(await _tokens_activos(tid)) == 1


async def test_aislamiento_entre_negocios(app_client) -> None:
    from core.db.models import PushToken
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug_a, slug_b = uniq("push-a"), uniq("push-b")
    tid_a = await _tenant(f"{slug_a}@test.local", slug_a)
    tid_b = await _tenant(f"{slug_b}@test.local", slug_b)
    token_a = await _login(app_client, f"{slug_a}@test.local")

    expo_token = f"ExponentPushToken[test-{uuid.uuid4().hex}]"
    r = await app_client.post(
        "/api/v1/me/push-token", json={"token": expo_token}, headers=_headers(token_a, tid_a)
    )
    assert r.status_code == 204

    # En el scope del negocio B (RLS) el token de A no existe.
    async with tenant_scope(tid_b):
        async with get_session() as session:
            visible = (
                await session.execute(
                    select(PushToken).where(PushToken.deleted_at.is_(None))
                )
            ).scalars().all()
    assert visible == []


async def test_token_invalido_es_422(app_client) -> None:
    slug = uniq("push-inv")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug)
    token = await _login(app_client, email)

    r = await app_client.post(
        "/api/v1/me/push-token", json={"token": "corto"}, headers=_headers(token, tid)
    )
    assert r.status_code == 422


async def test_token_de_otra_cuenta_se_reasigna_al_dispositivo(app_client) -> None:
    """ADR-0168: el token identifica el dispositivo; quien lo registra ahora lo recibe."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, PushToken, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("push-otro")
    email_a = f"{slug}-a@test.local"
    email_b = f"{slug}-b@test.local"
    tid = await _tenant(email_a, slug)
    async with get_session() as session:
        admin_b = Admin(email=email_b, password_hash=hash_password(PW), role="colaborador")
        session.add(admin_b)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin_b.id, role="colaborador"))
        await session.commit()
        id_b = admin_b.id

    token_a = await _login(app_client, email_a)
    token_b = await _login(app_client, email_b)
    expo_token = f"ExponentPushToken[test-{uuid.uuid4().hex}]"

    r = await app_client.post(
        "/api/v1/me/push-token", json={"token": expo_token}, headers=_headers(token_a, tid)
    )
    assert r.status_code == 204
    r = await app_client.post(
        "/api/v1/me/push-token", json={"token": expo_token}, headers=_headers(token_b, tid)
    )
    assert r.status_code == 204

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (
                await session.execute(select(PushToken).where(PushToken.token == expo_token))
            ).scalars().all()
    # Una sola fila viva, ahora de la cuenta B: la cuenta A deja de recibir en ese teléfono.
    assert len(filas) == 1
    assert filas[0].admin_id == id_b
    assert filas[0].deleted_at is None


async def test_sin_sesion_es_401(app_client) -> None:
    r = await app_client.post("/api/v1/me/push-token", json={"token": "ExponentPushToken[x]"})
    assert r.status_code == 401

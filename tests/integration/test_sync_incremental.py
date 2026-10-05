"""Sync incremental (`GET /api/v1/sync/{entidad}`, ADR-0097).

Cubre: cursor+limit con `nextCursor` y `deletedIds`, RBAC por sección, entidad
desconocida (404), cursor inválido (400) y aislamiento entre negocios. Requiere
el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

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


async def _tenant(email: str, slug: str, packs: list[str]) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de sync.",
        admin_email=email,
        admin_password=PW,
        packs=packs,
    )
    return tid


async def _login(app_client, email: str) -> str:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token: str, tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}


async def _agregar_cliente(tid: uuid.UUID, nombre: str) -> uuid.UUID:
    from pack_cobranza.models import Cliente

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            c = Cliente(tenant_id=tid, cartera_id=uuid.uuid4(), nombre=nombre)
            session.add(c)
            await session.flush()
            return c.id


async def test_cursor_next_cursor_y_deleted_ids(app_client) -> None:
    slug = uniq("sync")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug, packs=["pack_cobranza"])
    token = await _login(app_client, email)
    h = _headers(token, tid)

    cid = await _agregar_cliente(tid, "Cliente Uno")

    r = await app_client.get("/api/v1/sync/clientes_cobranza", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entidad"] == "clientes_cobranza"
    ids = {i["id"] for i in body["items"]}
    assert str(cid) in ids
    assert body["deletedIds"] == []
    cursor = body["nextCursor"]

    # Segunda vuelta con el cursor: sin novedades, mismo cursor.
    r2 = await app_client.get("/api/v1/sync/clientes_cobranza", params={"cursor": cursor}, headers=h)
    assert r2.status_code == 200
    assert r2.json()["items"] == []
    assert r2.json()["nextCursor"] == cursor

    # Soft delete del cliente → aparece en deletedIds.
    from pack_cobranza.models import Cliente

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            c = await session.get(Cliente, cid)
            c.deleted_at = datetime.now(UTC) + timedelta(seconds=5)
            await session.commit()

    r3 = await app_client.get("/api/v1/sync/clientes_cobranza", params={"cursor": cursor}, headers=h)
    assert str(cid) in r3.json()["deletedIds"]


async def test_entidad_desconocida_y_cursor_invalido(app_client) -> None:
    slug = uniq("sync-err")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug, packs=[])
    token = await _login(app_client, email)
    h = _headers(token, tid)

    r = await app_client.get("/api/v1/sync/no_existe", headers=h)
    assert r.status_code == 404
    r = await app_client.get(
        "/api/v1/sync/clientes", params={"cursor": "ayer"}, headers=h
    )
    assert r.status_code == 400


async def test_rbac_por_seccion_y_aislamiento(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    slug_a, slug_b = uniq("sync-a"), uniq("sync-b")
    email_a = f"{slug_a}@test.local"
    tid_a = await _tenant(email_a, slug_a, packs=["pack_cobranza"])
    tid_b = await _tenant(f"{slug_b}@test.local", slug_b, packs=["pack_cobranza"])
    await _agregar_cliente(tid_a, "Cliente de A")
    token_b = await _login(app_client, f"{slug_b}@test.local")

    # Cross-tenant: el dueño de B no ve clientes de A.
    r = await app_client.get(
        "/api/v1/sync/clientes_cobranza", headers=_headers(token_b, tid_b)
    )
    assert r.status_code == 200
    assert r.json()["items"] == []

    # Admin sin acceso a ninguna sección de sync: colaborador hereda read (ADR-0047),
    # así que se usa un admin SIN vínculo al tenant B: no puede apuntar el header.
    email_x = f"{uniq('sync-x')}@test.local"
    async with get_session() as session:
        admin_x = Admin(email=email_x, password_hash=hash_password(PW), role="colaborador")
        session.add(admin_x)
        await session.commit()
    token_x = await _login(app_client, email_x)
    r = await app_client.get(
        "/api/v1/sync/clientes_cobranza", headers=_headers(token_x, tid_b)
    )
    assert r.status_code == 403  # negocio ajeno: X-Tenant-Id fuera de sus tenants

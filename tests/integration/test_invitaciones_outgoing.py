"""HU-UI-14 · Invitaciones por enlace: listar las enviadas y revocarlas.

Con la BD real: `POST /api/v1/admin/invitations` crea una invitación a una identidad
existente; `GET /api/v1/admin/invitations/outgoing` lista las pendientes enviadas por el
negocio activo y `POST …/{id}/revoke` las retira. Un dueño no ve ni revoca las de otro
negocio; el master elige el negocio con `?tenant=`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "clave-segura-123"
OUTGOING = "/api/v1/admin/invitations/outgoing"
INVITAR = "/api/v1/admin/invitations"


async def _negocio(sufijo: str, owner_email: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Invit {sufijo}",
        slug=f"invit-{sufijo}",
        persona={"tone": "cercano"},
        admin_email=owner_email,
        admin_password=PW,
    )
    return tid


async def _admin(email: str) -> uuid.UUID:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        return admin.id


async def _headers(app_client, email: str, tenant_id: uuid.UUID) -> dict[str, str]:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "X-Tenant-Id": str(tenant_id)}


async def test_listar_y_revocar_las_invitaciones_enviadas(app_client) -> None:
    s = uniq("out")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    invitado = f"invitado-{s}@p0.test"
    await _admin(invitado)
    h = await _headers(app_client, owner, tid)

    # Invitar a una identidad existente.
    r = await app_client.post(INVITAR, headers=h, json={"email": invitado, "role": "colaborador"})
    assert r.status_code == 204, r.text

    salientes = (await app_client.get(OUTGOING, headers=h)).json()
    assert len(salientes) == 1
    assert salientes[0]["email"] == invitado
    assert salientes[0]["rol"] == "colaborador"

    # Revocar: desaparece de las salientes.
    r = await app_client.post(f"/api/v1/admin/invitations/{salientes[0]['id']}/revoke", headers=h)
    assert r.status_code == 204, r.text
    assert (await app_client.get(OUTGOING, headers=h)).json() == []


async def test_un_dueno_no_ve_ni_revoca_las_de_otro_negocio(app_client) -> None:
    s = uniq("iso-inv")
    owner_a, owner_b = f"a-{s}@p0.test", f"b-{s}@p0.test"
    ta = await _negocio(f"{s}a", owner_a)
    tb = await _negocio(f"{s}b", owner_b)
    invitado = f"invitado-{s}@p0.test"
    await _admin(invitado)

    hb = await _headers(app_client, owner_b, tb)
    await app_client.post(INVITAR, headers=hb, json={"email": invitado, "role": "colaborador"})

    # El dueño de A no ve la invitación del negocio B.
    ha = await _headers(app_client, owner_a, ta)
    assert (await app_client.get(OUTGOING, headers=ha)).json() == []

    # Y no puede revocarla (la resuelve el dueño de B o el master).
    from sqlalchemy import select

    from core.db.models import TenantInvitation
    from core.db.session import get_session

    async with get_session() as session:
        iid = (
            await session.execute(
                select(TenantInvitation.id).where(TenantInvitation.tenant_id == tb)
            )
        ).scalar_one()
    r = await app_client.post(f"/api/v1/admin/invitations/{iid}/revoke", headers=ha)
    assert r.status_code == 400, r.text

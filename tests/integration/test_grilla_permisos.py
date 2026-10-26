"""HU-UI-10 · Grilla de permisos: aplicar en bloque y leer el estado efectivo.

Con la BD real: `POST /api/v1/admin/collaborators/{id}/permissions` reemplaza en bloque las
filas explícitas de un colaborador en un negocio (las secciones ausentes vuelven al default del
rol); `GET /{id}/permissions` devuelve el acceso efectivo por sección y si es heredado. Un dueño
no toca a colaboradores de otro negocio ni las secciones de plataforma; nada cruza negocios.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "clave-segura-123"
ME = "/api/v1/admin/me"
PERMISOS = "/api/v1/admin/collaborators/{cid}/permissions"


async def _negocio(sufijo: str, owner_email: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Grilla {sufijo}",
        slug=f"grilla-{sufijo}",
        business_description="Negocio de prueba de la grilla.",
        persona={"tone": "cercano"},
        admin_email=owner_email,
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    return tid


async def _admin(email: str, role: str = "colaborador") -> uuid.UUID:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role=role)
        session.add(admin)
        await session.flush()
        return admin.id


async def _vincular(admin_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))


async def _headers(app_client, email: str, tenant_id: uuid.UUID | None = None) -> dict[str, str]:
    r = await app_client.post(
        "/api/v1/auth/login", json={"identifier": email, "password": PW}
    )
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    if tenant_id is not None:
        h["X-Tenant-Id"] = str(tenant_id)
    return h


async def _filas_explicitas(admin_id: uuid.UUID) -> list[tuple[str, str, uuid.UUID | None]]:
    from core.db.models import AdminPermission
    from core.db.session import get_session

    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    AdminPermission.section,
                    AdminPermission.access,
                    AdminPermission.tenant_id,
                ).where(AdminPermission.admin_id == admin_id)
            )
        ).all()
    return [(s, a, t) for s, a, t in rows]


async def test_grilla_aplica_en_bloque_y_lee_efectivo(app_client) -> None:
    s = uniq("grilla")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    colab = f"colab-{s}@p0.test"
    cid = await _admin(colab)
    await _vincular(cid, tid, "colaborador")
    h_owner = await _headers(app_client, owner, tid)

    # Estado inicial: todo lo hereda el rol (colaborador → read), nada explícito.
    estado = (await app_client.get(PERMISOS.format(cid=cid), headers=h_owner)).json()
    por_seccion = {e["section"]: e for e in estado}
    assert por_seccion["conversations"] == {"section": "conversations", "access": "read", "heredado": True}
    assert por_seccion["pack_cobranza"] == {"section": "pack_cobranza", "access": "read", "heredado": True}
    # Plataforma y gestión de cuentas no se heredan.
    assert por_seccion["tenants"]["access"] == "none"
    assert por_seccion["collaborators"]["access"] == "none"

    # Aplicar en bloque: write en conversaciones, none en cobranza, read en costs.
    r = await app_client.post(
        PERMISOS.format(cid=cid),
        headers=h_owner,
        json={"permissions": {"conversations": "write", "pack_cobranza": "none", "costs": "read"}},
    )
    assert r.status_code == 204, r.text

    me = (await app_client.get(ME, headers=await _headers(app_client, colab, tid))).json()
    assert me["permissions"]["conversations"] == "write"
    assert me["permissions"]["costs"] == "read"
    assert all(
        me["permissions"][k] is None for k in me["permissions"] if k.startswith("pack_cobranza")
    ), "denegar el pack saca sus sub-secciones"

    estado = (await app_client.get(PERMISOS.format(cid=cid), headers=h_owner)).json()
    por_seccion = {e["section"]: e for e in estado}
    assert por_seccion["conversations"] == {"section": "conversations", "access": "write", "heredado": False}
    assert por_seccion["pack_cobranza"] == {"section": "pack_cobranza", "access": "none", "heredado": False}
    assert por_seccion["costs"] == {"section": "costs", "access": "read", "heredado": False}
    assert por_seccion["leads"]["access"] == "read" and por_seccion["leads"]["heredado"], "lo ausente hereda"


async def test_grilla_reemplaza_las_filas_del_negocio(app_client) -> None:
    s = uniq("reempl")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    colab = f"colab-{s}@p0.test"
    cid = await _admin(colab)
    await _vincular(cid, tid, "colaborador")
    h_owner = await _headers(app_client, owner, tid)

    await app_client.post(
        PERMISOS.format(cid=cid),
        headers=h_owner,
        json={"permissions": {"conversations": "write", "costs": "read"}},
    )
    # Un segundo guardado sin `costs`: la fila de costs desaparece (vuelve al default).
    r = await app_client.post(
        PERMISOS.format(cid=cid),
        headers=h_owner,
        json={"permissions": {"conversations": "read"}},
    )
    assert r.status_code == 204, r.text
    filas = await _filas_explicitas(cid)
    assert [(s, a) for s, a, _ in filas] == [("conversations", "read")], filas


async def test_dueno_no_toca_otro_negocio_ni_secciones_de_plataforma(app_client) -> None:
    s = uniq("iso")
    owner_a, owner_b = f"a-{s}@p0.test", f"b-{s}@p0.test"
    ta = await _negocio(f"{s}a", owner_a)
    tb = await _negocio(f"{s}b", owner_b)
    colab_b = f"colab-b-{s}@p0.test"
    cid_b = await _admin(colab_b)
    await _vincular(cid_b, tb, "colaborador")

    # El dueño de A no gestiona al colaborador de B.
    h_owner_a = await _headers(app_client, owner_a, ta)
    r = await app_client.post(
        PERMISOS.format(cid=cid_b),
        headers=h_owner_a,
        json={"permissions": {"conversations": "write"}},
    )
    assert r.status_code == 400, r.text

    # El dueño no otorga la gestión de cuentas (OWNER_GRANTABLE_SECTIONS).
    colab_a = f"colab-a-{s}@p0.test"
    cid_a = await _admin(colab_a)
    await _vincular(cid_a, ta, "colaborador")
    r = await app_client.post(
        PERMISOS.format(cid=cid_a),
        headers=h_owner_a,
        json={"permissions": {"collaborators": "write"}},
    )
    assert r.status_code == 400, r.text


async def test_dueno_solo_lee_permisos_de_cuentas_de_su_negocio(app_client) -> None:
    """Auditoría multitenant 2026-10-26: `GET /{id}/permissions` ya no responde por cualquier
    admin de la plataforma. Una cuenta de otro negocio y una inexistente dan la misma respuesta."""
    s = uniq("lec")
    owner_a, owner_b = f"a-{s}@p0.test", f"b-{s}@p0.test"
    ta = await _negocio(f"{s}a", owner_a)
    tb = await _negocio(f"{s}b", owner_b)
    colab_a, colab_b = f"colab-a-{s}@p0.test", f"colab-b-{s}@p0.test"
    cid_a, cid_b = await _admin(colab_a), await _admin(colab_b)
    await _vincular(cid_a, ta, "colaborador")
    await _vincular(cid_b, tb, "colaborador")
    h_owner_a = await _headers(app_client, owner_a, ta)

    # Su colaborador: sí lo lee.
    propio = await app_client.get(PERMISOS.format(cid=cid_a), headers=h_owner_a)
    assert propio.status_code == 200, propio.text
    assert propio.json(), "devuelve el acceso por sección"

    # El de otro negocio y un UUID inexistente: 400 idéntico, sin revelar si la cuenta existe.
    ajeno = await app_client.get(PERMISOS.format(cid=cid_b), headers=h_owner_a)
    inexistente = await app_client.get(PERMISOS.format(cid=uuid.uuid4()), headers=h_owner_a)
    assert ajeno.status_code == inexistente.status_code == 400, (ajeno.text, inexistente.text)
    assert ajeno.json() == inexistente.json()

    # El master sigue pudiendo leer el estado de cualquier cuenta eligiendo el negocio.
    from _api_helpers import api_headers

    h_master = await api_headers(app_client, tb)
    assert (await app_client.get(PERMISOS.format(cid=cid_b), headers=h_master)).status_code == 200

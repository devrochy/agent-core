"""P0 de seguridad del ciclo de pruebas 2026-10-13 (ADR-0136), por la API `/api/v1`.

1. Negocio suspendido: su dueño y sus colaboradores no inician sesión (403 `tenant_suspended`)
   y una sesión ya abierta responde 403; el master sí entra; con dos negocios, el activo sigue.
2. Permisos: revocar una sección (o un pack completo) a un colaborador la quita de `/admin/me`
   y da 403 en su API; en otro negocio el mismo colaborador conserva su acceso.
3. Cuenta desactivada: sus sesiones mueren en la misma transacción y no resucitan al reactivar;
   cambiar la contraseña desde Colaboradores también cierra sesiones.
4. Propietarios pares y desvincular: un owner invitado no toca al owner real (ni al revés);
   `unlink` saca a un colaborador del negocio activo, nunca a un owner ni a alguien de otro negocio.
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
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/admin/me"
REPORTES = "/api/v1/pack/cobranza/reports"
CARTERAS = "/api/v1/pack/cobranza/portfolios"


async def _negocio(sufijo: str, owner_email: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"P0 {sufijo}",
        slug=f"p0-{sufijo}",
        business_description="Negocio de prueba P0.",
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


async def _estado(tenant_id: uuid.UUID, status: str) -> None:
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        (await session.get(Tenant, tenant_id)).status = status


async def _admin_id(email: str) -> uuid.UUID:
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(select(Admin.id).where(Admin.email == email))).scalar_one()


async def _login(app_client, email: str, password: str = PW):
    return await app_client.post(LOGIN, json={"identifier": email, "password": password})


async def _headers(app_client, email: str, tenant_id: uuid.UUID | None = None) -> dict[str, str]:
    r = await _login(app_client, email)
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    if tenant_id is not None:
        h["X-Tenant-Id"] = str(tenant_id)
    return h


# ------------------------------------------------------------------ 1. suspendido
async def test_negocio_suspendido_no_deja_entrar_al_dueno_ni_al_colaborador(app_client) -> None:
    s = uniq("susp")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    colab = f"colab-{s}@p0.test"
    await _vincular(await _admin(colab), tid, "colaborador")

    # Sesiones abiertas ANTES de suspender.
    h_owner = await _headers(app_client, owner, tid)
    h_colab = await _headers(app_client, colab, tid)
    assert (await app_client.get(ME, headers=h_owner)).status_code == 200

    await _estado(tid, "suspended")
    for h in (h_owner, h_colab):
        r = await app_client.get(ME, headers=h)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "tenant_suspended"
    # Puede cerrar sesión igual.
    assert (await app_client.post("/api/v1/auth/logout", headers=h_owner)).status_code == 204

    # Login nuevo: 403 con código, sin crear sesión; contraseña mala sigue siendo 401.
    r = await _login(app_client, owner)
    assert r.status_code == 403 and r.json()["error"]["code"] == "tenant_suspended"
    assert (await _login(app_client, colab)).status_code == 403
    assert (await _login(app_client, owner, "mala-clave-000")).status_code == 401
    from core.db.models import AdminSession
    from core.db.session import get_session

    async with get_session() as session:
        abiertas = (
            await session.execute(select(AdminSession).where(AdminSession.admin_id == await _admin_id(owner)))
        ).scalars().all()
    assert abiertas == [], "no se crea sesión para un dueño con el negocio suspendido"

    # El master entra y lo reactiva desde la plataforma.
    h_master = await _headers_master(app_client)
    assert (await app_client.get(ME, headers=h_master)).status_code == 200
    r = await app_client.post(f"/api/v1/admin/platform/tenants/{tid}/activate", headers=h_master)
    assert r.status_code == 204, r.text

    # Reactivado: el dueño vuelve a entrar.
    assert (await _login(app_client, owner)).status_code == 200


async def _headers_master(app_client) -> dict[str, str]:
    r = await app_client.post(LOGIN, json={"identifier": "admin@local", "password": os.environ.get("TEST_ADMIN_PASSWORD", "admin123")})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_con_dos_negocios_solo_se_corta_el_suspendido(app_client) -> None:
    s = uniq("dos")
    owner_a, owner_b = f"a-{s}@p0.test", f"b-{s}@p0.test"
    ta = await _negocio(f"{s}a", owner_a)
    tb = await _negocio(f"{s}b", owner_b)
    # El dueño de A también es colaborador de B.
    await _vincular(await _admin_id(owner_a), tb, "colaborador")
    await _estado(ta, "suspended")

    r = await _login(app_client, owner_a)
    assert r.status_code == 200, "entra: le queda un negocio activo"
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    r = await app_client.get(ME, headers={**h, "X-Tenant-Id": str(ta)})
    assert r.status_code == 403 and r.json()["error"]["code"] == "tenant_suspended"
    r = await app_client.get(ME, headers=h)
    assert r.status_code == 200 and r.json()["tenant"]["id"] == str(tb) and r.json()["tenant_count"] == 1
    lista = await app_client.get("/api/v1/admin/tenants", headers=h)
    assert [t["id"] for t in lista.json()["items"]] == [str(tb)]
    await _estado(ta, "active")


# ------------------------------------------------------------------ 2. permisos
async def test_revocar_una_seccion_y_un_pack_completo_se_aplica_en_me_y_en_la_api(app_client) -> None:
    s = uniq("perm")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    colab = f"colab-{s}@p0.test"
    cid = await _admin(colab)
    await _vincular(cid, tid, "colaborador")
    h_owner = await _headers(app_client, owner, tid)
    h_colab = await _headers(app_client, colab, tid)

    me = (await app_client.get(ME, headers=h_colab)).json()
    assert me["permissions"]["pack_cobranza:reportes"] == "read", "default del colaborador (ADR-0047)"
    assert me["permissions"]["pack_cobranza"] == "read", "/me expone también el acceso al pack"
    assert (await app_client.get(REPORTES, headers=h_colab)).status_code == 200

    # Quitar Reportes: desaparece de /me y la API da 403; Carteras sigue.
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{cid}/revoke", headers=h_owner, json={"section": "pack_cobranza:reportes"}
    )
    assert r.status_code == 204, r.text
    me = (await app_client.get(ME, headers=h_colab)).json()
    assert me["permissions"]["pack_cobranza:reportes"] is None
    assert me["permissions"]["pack_cobranza:carteras"] == "read"
    assert (await app_client.get(REPORTES, headers=h_colab)).status_code == 403
    assert (await app_client.get(CARTERAS, headers=h_colab)).status_code == 200
    fila = next(
        p for a in (await app_client.get("/api/v1/admin/collaborators", headers=h_owner)).json()
        if a["id"] == str(cid) for p in a["permissions"]
    )
    assert (fila["section"], fila["access"]) == ("pack_cobranza:reportes", "none")

    # Quitar cobranza en general: todas las sub-secciones fuera.
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{cid}/revoke", headers=h_owner, json={"section": "pack_cobranza"}
    )
    assert r.status_code == 204, r.text
    me = (await app_client.get(ME, headers=h_colab)).json()
    assert all(me["permissions"][k] is None for k in me["permissions"] if k.startswith("pack_cobranza"))
    assert me["permissions"]["conversations"] == "read", "el resto no cambia"
    assert (await app_client.get(CARTERAS, headers=h_colab)).status_code == 403

    # Devolver solo Reportes en lectura: la sub-sección manda sobre el pack denegado.
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{cid}/grant",
        headers=h_owner,
        json={"section": "pack_cobranza:reportes", "access": "read"},
    )
    assert r.status_code == 204, r.text
    me = (await app_client.get(ME, headers=h_colab)).json()
    assert me["permissions"]["pack_cobranza:reportes"] == "read"
    assert me["permissions"]["pack_cobranza:carteras"] is None
    assert (await app_client.get(REPORTES, headers=h_colab)).status_code == 200

    # Una denegación global no existe.
    h_master = await _headers_master(app_client)
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{cid}/grant", headers=h_master, json={"section": "costs", "access": "none"}
    )
    assert r.status_code == 400


async def test_la_denegacion_es_por_negocio(app_client) -> None:
    """El mismo colaborador en dos negocios: lo que A le quita no afecta a B, y el dueño de B
    no puede tocar sus permisos de A."""
    s = uniq("cruz")
    owner_a, owner_b = f"a-{s}@p0.test", f"b-{s}@p0.test"
    ta = await _negocio(f"{s}a", owner_a)
    tb = await _negocio(f"{s}b", owner_b)
    colab = f"colab-{s}@p0.test"
    cid = await _admin(colab)
    await _vincular(cid, ta, "colaborador")
    await _vincular(cid, tb, "colaborador")
    h_a = await _headers(app_client, owner_a, ta)
    h_b = await _headers(app_client, owner_b, tb)

    # Cuenta compartida: ningún dueño la gestiona por sí solo (sigue siendo del master).
    r = await app_client.post(f"/api/v1/admin/collaborators/{cid}/revoke", headers=h_a, json={"section": "pack_cobranza"})
    assert r.status_code == 400
    # El master la deniega en A nada más.
    h_master = await _headers_master(app_client)
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{cid}/revoke",
        headers=h_master,
        json={"section": "pack_cobranza", "tenant": f"p0-{s}a"},
    )
    assert r.status_code == 204, r.text
    tok = (await _login(app_client, colab)).json()["token"]
    en_a = (await app_client.get(ME, headers={"Authorization": f"Bearer {tok}", "X-Tenant-Id": str(ta)})).json()
    en_b = (await app_client.get(ME, headers={"Authorization": f"Bearer {tok}", "X-Tenant-Id": str(tb)})).json()
    assert en_a["permissions"]["pack_cobranza:carteras"] is None
    assert en_b["permissions"]["pack_cobranza:carteras"] == "read"
    assert (await app_client.get(CARTERAS, headers={"Authorization": f"Bearer {tok}", "X-Tenant-Id": str(tb)})).status_code == 200
    # El dueño de B tampoco ve ni toca al colaborador por el alcance de A.
    r = await app_client.post(f"/api/v1/admin/collaborators/{cid}/toggle", headers=h_b)
    assert r.status_code == 400


# ------------------------------------------------------------------ 3. sesión
async def test_desactivar_cierra_la_sesion_y_reactivar_no_la_resucita(app_client) -> None:
    s = uniq("ses")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    colab = f"colab-{s}@p0.test"
    cid = await _admin(colab)
    await _vincular(cid, tid, "colaborador")
    h_owner = await _headers(app_client, owner, tid)
    h_colab = await _headers(app_client, colab, tid)
    assert (await app_client.get(ME, headers=h_colab)).status_code == 200

    assert (await app_client.post(f"/api/v1/admin/collaborators/{cid}/toggle", headers=h_owner)).status_code == 204
    assert (await app_client.get(ME, headers=h_colab)).status_code == 401
    assert (await _login(app_client, colab)).status_code == 401
    # Reactivar: el token viejo sigue muerto; hay que volver a entrar.
    assert (await app_client.post(f"/api/v1/admin/collaborators/{cid}/toggle", headers=h_owner)).status_code == 204
    assert (await app_client.get(ME, headers=h_colab)).status_code == 401
    h_colab = await _headers(app_client, colab, tid)
    assert (await app_client.get(ME, headers=h_colab)).status_code == 200

    # Cambiar la contraseña desde Colaboradores también cierra la sesión.
    r = await app_client.post(
        f"/api/v1/admin/collaborators/{cid}/password", headers=h_owner, json={"password": "otra-clave-456"}
    )
    assert r.status_code == 204
    assert (await app_client.get(ME, headers=h_colab)).status_code == 401
    assert (await _login(app_client, colab, "otra-clave-456")).status_code == 200


# ------------------------------------------------------------------ 4. propietarios y unlink
async def test_owner_invitado_no_toca_al_owner_real_y_unlink_con_guardas(app_client) -> None:
    s = uniq("own")
    real = f"real-{s}@p0.test"
    tid = await _negocio(s, real)
    invitado = f"invitado-{s}@p0.test"
    iid = await _admin(invitado)
    await _vincular(iid, tid, "owner")
    colab = f"colab-{s}@p0.test"
    cid = await _admin(colab)
    await _vincular(cid, tid, "colaborador")
    rid = await _admin_id(real)
    h_real = await _headers(app_client, real, tid)
    h_inv = await _headers(app_client, invitado, tid)
    col = "/api/v1/admin/collaborators"

    # El invitado no modifica al real; el real tampoco al invitado (pares: lo hace el master).
    for actor, objetivo in ((h_inv, rid), (h_real, iid)):
        assert (await app_client.post(f"{col}/{objetivo}/password", headers=actor, json={"password": "x" * 10})).status_code == 400
        assert (await app_client.post(f"{col}/{objetivo}/toggle", headers=actor)).status_code == 400
        assert (await app_client.post(f"{col}/{objetivo}/grant", headers=actor, json={"section": "costs", "access": "write"})).status_code == 400
        assert (await app_client.post(f"{col}/{objetivo}/unlink", headers=actor, json={"tenant": f"p0-{s}"})).status_code == 400
    assert (await _login(app_client, real)).status_code == 200, "el real sigue intacto"
    # Un owner no crea otro owner.
    r = await app_client.post(col, headers=h_inv, json={"email": f"otro-{s}@p0.test", "password": PW, "role": "owner"})
    assert r.status_code == 400
    # Al colaborador sí lo gestiona cualquiera de los dos.
    assert (await app_client.post(f"{col}/{cid}/grant", headers=h_inv, json={"section": "costs", "access": "write"})).status_code == 204

    # Unlink: no a uno mismo, no desde otro negocio, sí al colaborador del negocio activo.
    assert (await app_client.post(f"{col}/{iid}/unlink", headers=h_inv, json={"tenant": f"p0-{s}"})).status_code == 400
    otro = f"otro-{s}@p0.test"
    to = await _negocio(f"{s}o", otro)
    h_otro = await _headers(app_client, otro, to)
    assert (await app_client.post(f"{col}/{cid}/unlink", headers=h_otro, json={"tenant": f"p0-{s}"})).status_code == 400
    r = await app_client.post(f"{col}/{cid}/unlink", headers=h_real, json={"tenant": f"p0-{s}"})
    assert r.status_code == 204, r.text
    from core.db.models import AdminPermission, TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        assert (await session.execute(select(TenantUser).where(TenantUser.admin_id == cid, TenantUser.tenant_id == tid))).scalar_one_or_none() is None
        assert (await session.execute(select(AdminPermission).where(AdminPermission.admin_id == cid, AdminPermission.tenant_id == tid))).scalars().all() == []
    # El colaborador ya no accede a ese negocio.
    r = await app_client.get(ME, headers={"Authorization": (await _headers(app_client, colab))["Authorization"], "X-Tenant-Id": str(tid)})
    assert r.status_code == 403
    assert str(cid) not in [a["id"] for a in (await app_client.get(col, headers=h_real)).json()]

    # El master desvincula owners, pero no al último.
    h_master = await _headers_master(app_client)
    assert (await app_client.post(f"{col}/{iid}/unlink", headers=h_master, json={"tenant": f"p0-{s}"})).status_code == 204
    r = await app_client.post(f"{col}/{rid}/unlink", headers=h_master, json={"tenant": f"p0-{s}"})
    assert r.status_code == 400 and "sin propietario" in r.json()["error"]["message"]


async def test_unlink_de_cuenta_compartida_no_toca_el_otro_negocio_y_revoca_invitaciones(app_client) -> None:
    """Revisión del PR: el owner de A desvincula de A a un colaborador que también está en B; B queda
    intacto, y la invitación pendiente de A a esa cuenta se revoca (aceptarla desharía el unlink).
    El owner de A tampoco alcanza B mandando el slug de B en el payload."""
    from core.db.models import AdminPermission, TenantInvitation, TenantUser
    from core.db.session import get_session

    s = uniq("comp")
    owner_a, owner_b = f"a-{s}@p0.test", f"b-{s}@p0.test"
    ta = await _negocio(f"{s}a", owner_a)
    tb = await _negocio(f"{s}b", owner_b)
    compartido = await _admin(f"comp-{s}@p0.test")
    await _vincular(compartido, ta, "colaborador")
    await _vincular(compartido, tb, "colaborador")
    exclusivo_b = await _admin(f"excl-{s}@p0.test")
    await _vincular(exclusivo_b, tb, "colaborador")
    async with get_session() as session:
        session.add(AdminPermission(admin_id=compartido, section="costs", access="read", tenant_id=tb))
        session.add(TenantInvitation(tenant_id=ta, admin_id=compartido, role="colaborador", estado="pendiente"))
    h_a = await _headers(app_client, owner_a, ta)
    col = "/api/v1/admin/collaborators"

    # Owner de A con el slug de B: ni grant/revoke/unlink sobre el exclusivo de B.
    for ruta, body in (
        ("grant", {"section": "costs", "access": "write", "tenant": f"p0-{s}b"}),
        ("revoke", {"section": "costs", "tenant": f"p0-{s}b"}),
        ("unlink", {"tenant": f"p0-{s}b"}),
    ):
        r = await app_client.post(f"{col}/{exclusivo_b}/{ruta}", headers=h_a, json=body)
        assert r.status_code == 400, (ruta, r.text)
    async with get_session() as session:
        filas_b = (
            await session.execute(select(AdminPermission).where(AdminPermission.admin_id == exclusivo_b))
        ).scalars().all()
    assert filas_b == []

    r = await app_client.post(f"{col}/{compartido}/unlink", headers=h_a, json={"tenant": f"p0-{s}a"})
    assert r.status_code == 204, r.text
    async with get_session() as session:
        vinculos = (
            await session.execute(select(TenantUser.tenant_id).where(TenantUser.admin_id == compartido))
        ).scalars().all()
        permisos_b = (
            await session.execute(
                select(AdminPermission.section).where(
                    AdminPermission.admin_id == compartido, AdminPermission.tenant_id == tb
                )
            )
        ).scalars().all()
        invitacion = (
            await session.execute(
                select(TenantInvitation.estado).where(
                    TenantInvitation.admin_id == compartido, TenantInvitation.tenant_id == ta
                )
            )
        ).scalar_one()
    assert vinculos == [tb]
    assert permisos_b == ["costs"]
    assert invitacion == "revocada"


async def test_el_check_de_0090_rechaza_una_denegacion_global() -> None:
    from sqlalchemy.exc import IntegrityError

    from core.db.models import AdminPermission
    from core.db.session import get_session

    aid = await _admin(f"chk-{uniq('chk')}@p0.test")
    with pytest.raises(IntegrityError):
        async with get_session() as session:
            session.add(AdminPermission(admin_id=aid, section="costs", access="none", tenant_id=None))


async def test_notificaciones_usan_la_misma_jerarquia(app_client) -> None:
    """`notification_recipients`: denegar el pack saca al owner de la sub-sección; conceder el pack
    incluye al colaborador; la sub-sección manda sobre el pack."""
    from core.admin.permissions import notification_recipients
    from core.db.models import AdminPermission
    from core.db.session import get_session

    s = uniq("notif")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    colab = f"colab-{s}@p0.test"
    cid = await _admin(colab)
    await _vincular(cid, tid, "colaborador")
    oid = await _admin_id(owner)
    sub = "pack_calendar:notificaciones"

    dest = await notification_recipients(sub, tid)
    assert owner in dest and colab not in dest
    async with get_session() as session:
        session.add(AdminPermission(admin_id=oid, section="pack_calendar", access="none", tenant_id=tid))
        session.add(AdminPermission(admin_id=cid, section="pack_calendar", access="read", tenant_id=tid))
    dest = await notification_recipients(sub, tid)
    assert owner not in dest and colab in dest
    async with get_session() as session:
        session.add(AdminPermission(admin_id=oid, section=sub, access="write", tenant_id=tid))
    assert owner in await notification_recipients(sub, tid)


async def test_app_de_campo_corta_al_desvincular_y_al_suspender(app_client) -> None:
    """Revisión del PR: el access de campo, el refresh y la API de dispositivo (`api.py`) dejan de
    aceptar a una cuenta desvinculada o de un negocio suspendido; el refresh no copia el rol viejo."""
    s = uniq("campo")
    owner = f"owner-{s}@p0.test"
    tid = await _negocio(s, owner)
    co = f"co-{s}@p0.test"
    co_id = await _admin(co)
    await _vincular(co_id, tid, "owner")
    campo = "/api/pack/pack_cobranza"

    async def _tokens(email: str) -> dict:
        r = await app_client.post(f"{campo}/auth/login", json={"usuario": email, "password": PW, "tenant": f"p0-{s}"})
        assert r.status_code == 200, r.text
        return r.json()

    t_co = await _tokens(co)
    assert t_co["rol"] == "propietario"
    h_co = {"Authorization": f"Bearer {t_co['accessToken']}"}
    assert (await app_client.get(f"{campo}/gestor/mis-carteras", headers=h_co)).status_code == 200
    assert (await app_client.get(f"{campo}/mis-carteras", headers=h_co)).status_code == 200
    # El refresh no sirve como acceso en ninguna de las dos APIs.
    h_ref = {"Authorization": f"Bearer {t_co['refreshToken']}"}
    assert (await app_client.get(f"{campo}/gestor/mis-carteras", headers=h_ref)).status_code == 401
    assert (await app_client.get(f"{campo}/mis-carteras", headers=h_ref)).status_code == 401

    # El master lo desvincula: access, refresh y API de dispositivo fuera.
    h_master = await _headers_master(app_client)
    r = await app_client.post(f"/api/v1/admin/collaborators/{co_id}/unlink", headers=h_master, json={"tenant": f"p0-{s}"})
    assert r.status_code == 204, r.text
    assert (await app_client.get(f"{campo}/gestor/mis-carteras", headers=h_co)).status_code == 401
    assert (await app_client.get(f"{campo}/mis-carteras", headers=h_co)).status_code == 401
    r = await app_client.post(f"{campo}/auth/refresh", json={"refreshToken": t_co["refreshToken"]})
    assert r.status_code == 401

    # Suspender corta al dueño (access y refresh); reactivar lo devuelve con su rol actual.
    t_ow = await _tokens(owner)
    h_ow = {"Authorization": f"Bearer {t_ow['accessToken']}"}
    await _estado(tid, "suspended")
    assert (await app_client.get(f"{campo}/gestor/mis-carteras", headers=h_ow)).status_code == 401
    assert (await app_client.post(f"{campo}/auth/refresh", json={"refreshToken": t_ow["refreshToken"]})).status_code == 401
    await _estado(tid, "active")
    r = await app_client.post(f"{campo}/auth/refresh", json={"refreshToken": t_ow["refreshToken"]})
    assert r.status_code == 200, r.text
    assert r.json()["accessToken"]

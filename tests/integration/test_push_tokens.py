"""Registro de dispositivos de push (`/api/v1/me/push-token`, ADR-0099).

Cubre: alta idempotente, reactivación tras baja, baja por soft delete,
aislamiento RLS entre negocios, validación de payload y la liberación del token
en otros negocios y cuentas (ADR-0168, `push_token_release_elsewhere`). Requiere
el stack local (Postgres + Redis).
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


# --- Un teléfono, un solo negocio y una sola cuenta (ADR-0168, migración 0109) -----------


async def _filas(tid: uuid.UUID, token: str):
    """Todas las filas (activas o dadas de baja) del token en ESE negocio, vía RLS."""
    from core.db.models import PushToken
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(select(PushToken).where(PushToken.token == token))
            ).scalars().all()


def _nuevo_token() -> str:
    return f"ExponentPushToken[test-{uuid.uuid4().hex}]"


async def _alta(app_client, jwt: str, tid: uuid.UUID, token: str) -> None:
    r = await app_client.post("/api/v1/me/push-token", json={"token": token}, headers=_headers(jwt, tid))
    assert r.status_code == 204, r.text


@pytest.mark.aislamiento
async def test_mismo_token_en_otro_negocio_y_otra_cuenta_libera_el_anterior(app_client) -> None:
    """El caso del OPPO: sesión vieja en el negocio A (cuenta X), sesión nueva en B (cuenta Y)."""
    slug_a, slug_b = uniq("push-lib-a"), uniq("push-lib-b")
    email_a, email_b = f"{slug_a}@test.local", f"{slug_b}@test.local"
    tid_a, tid_b = await _tenant(email_a, slug_a), await _tenant(email_b, slug_b)
    jwt_a, jwt_b = await _login(app_client, email_a), await _login(app_client, email_b)
    expo = _nuevo_token()

    await _alta(app_client, jwt_a, tid_a, expo)
    assert [f.deleted_at is None for f in await _filas(tid_a, expo)] == [True]

    await _alta(app_client, jwt_b, tid_b, expo)

    # Activo solo en B; en A la fila sigue existiendo pero dada de baja (soft delete).
    fila_a = await _filas(tid_a, expo)
    assert len(fila_a) == 1 and fila_a[0].deleted_at is not None
    fila_b = await _filas(tid_b, expo)
    assert len(fila_b) == 1 and fila_b[0].deleted_at is None


@pytest.mark.aislamiento
async def test_misma_cuenta_en_dos_negocios_deja_el_token_en_el_ultimo(app_client) -> None:
    """La cuenta con acceso a dos negocios que cambia de negocio en la app: gana el activo."""
    from sqlalchemy import select as sel

    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    slug_a, slug_b = uniq("push-dos-a"), uniq("push-dos-b")
    email_a = f"{slug_a}@test.local"
    tid_a, tid_b = await _tenant(email_a, slug_a), await _tenant(f"{slug_b}@test.local", slug_b)
    async with get_session() as session:
        admin_a = (await session.execute(sel(Admin).where(Admin.email == email_a))).scalar_one()
        session.add(TenantUser(tenant_id=tid_b, admin_id=admin_a.id, role="colaborador"))
        await session.commit()
    jwt = await _login(app_client, email_a)
    expo = _nuevo_token()

    await _alta(app_client, jwt, tid_a, expo)
    await _alta(app_client, jwt, tid_b, expo)
    assert [f.deleted_at is not None for f in await _filas(tid_a, expo)] == [True]
    assert [f.deleted_at is None for f in await _filas(tid_b, expo)] == [True]

    # Volver al negocio A reactiva A y libera B: siempre uno solo activo.
    await _alta(app_client, jwt, tid_a, expo)
    assert [f.deleted_at is None for f in await _filas(tid_a, expo)] == [True]
    assert [f.deleted_at is not None for f in await _filas(tid_b, expo)] == [True]


@pytest.mark.aislamiento
async def test_alta_repetida_misma_cuenta_y_negocio_es_idempotente(app_client) -> None:
    slug = uniq("push-idem")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug)
    jwt = await _login(app_client, email)
    expo = _nuevo_token()

    for _ in range(3):
        await _alta(app_client, jwt, tid, expo)
    filas = await _filas(tid, expo)
    assert len(filas) == 1 and filas[0].deleted_at is None


@pytest.mark.aislamiento
async def test_no_toca_otros_tokens_ni_otros_negocios_si_el_token_no_coincide(app_client) -> None:
    slug_a, slug_b = uniq("push-ajeno-a"), uniq("push-ajeno-b")
    email_a, email_b = f"{slug_a}@test.local", f"{slug_b}@test.local"
    tid_a, tid_b = await _tenant(email_a, slug_a), await _tenant(email_b, slug_b)
    jwt_a, jwt_b = await _login(app_client, email_a), await _login(app_client, email_b)
    token_a, token_a2, token_b = _nuevo_token(), _nuevo_token(), _nuevo_token()

    await _alta(app_client, jwt_a, tid_a, token_a)
    await _alta(app_client, jwt_a, tid_a, token_a2)  # otro teléfono del mismo negocio y cuenta
    await _alta(app_client, jwt_b, tid_b, token_b)  # token distinto en otro negocio

    for tid, token in ((tid_a, token_a), (tid_a, token_a2), (tid_b, token_b)):
        filas = await _filas(tid, token)
        assert len(filas) == 1 and filas[0].deleted_at is None, token


@pytest.mark.aislamiento
async def test_la_funcion_no_hace_nada_con_parametros_vacios_y_la_rls_sigue_cerrada(app_client) -> None:
    from sqlalchemy import text

    from core.db.models import PushToken
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug_a, slug_b = uniq("push-nul-a"), uniq("push-nul-b")
    email_a = f"{slug_a}@test.local"
    tid_a, tid_b = await _tenant(email_a, slug_a), await _tenant(f"{slug_b}@test.local", slug_b)
    jwt_a = await _login(app_client, email_a)
    expo = _nuevo_token()
    await _alta(app_client, jwt_a, tid_a, expo)

    async def liberar(token, tenant, admin) -> int:
        async with tenant_scope(tid_b):
            async with get_session() as session:
                n = (
                    await session.execute(
                        text("select push_token_release_elsewhere(:t, :tn, :a)"),
                        {"t": token, "tn": tenant, "a": admin},
                    )
                ).scalar_one()
                await session.commit()
                return n

    # NULL en el negocio o la cuenta a conservar, o token vacío: no libera nada.
    assert await liberar(expo, None, uuid.uuid4()) == 0
    assert await liberar(expo, tid_b, None) == 0
    assert await liberar("", tid_b, uuid.uuid4()) == 0
    assert [f.deleted_at is None for f in await _filas(tid_a, expo)] == [True]

    # RLS intacta: desde el negocio B (con la función ya ejecutada) las filas de A no se ven,
    # y un UPDATE directo tampoco las toca.
    async with tenant_scope(tid_b):
        async with get_session() as session:
            assert (await session.execute(select(PushToken).where(PushToken.token == expo))).all() == []
            res = await session.execute(
                text("update push_tokens set deleted_at = now() where token = :t"), {"t": expo}
            )
            assert res.rowcount == 0
            await session.commit()
    assert [f.deleted_at is None for f in await _filas(tid_a, expo)] == [True]

    # Con un negocio y cuenta a conservar distintos, la función sí libera (conteo = 1).
    assert await liberar(expo, tid_b, uuid.uuid4()) == 1
    assert [f.deleted_at is not None for f in await _filas(tid_a, expo)] == [True]

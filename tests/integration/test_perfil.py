"""Perfil propio (SPEC-34): leer/editar nombre y teléfono, usuario único, contraseña y avatar.

Con la BD real: `PATCH /admin/me` actualiza el perfil del usuario autenticado, `POST
/me/password` pide la contraseña actual y `POST/DELETE /me/avatar` guardan o quitan la foto
por el controlador único de almacenamiento. Nada de esto toca el perfil de otra cuenta.
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

PW = "clave-segura-123"
ME = "/api/v1/admin/me"
AVATAR = "/api/v1/admin/me/avatar"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _negocio(sufijo: str, owner_email: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Perfil {sufijo}",
        slug=f"perfil-{sufijo}",
        business_description="Negocio de prueba del perfil.",
        persona={"tone": "cercano"},
        admin_email=owner_email,
        admin_password=PW,
        packs=[],
    )
    return tid


async def _headers(app_client, email: str, password: str = PW) -> dict[str, str]:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _admin_con_username(email: str, username: str) -> uuid.UUID:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = Admin(email=email, username=username, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        return admin.id


async def test_perfil_se_lee_y_actualiza(app_client) -> None:
    s = uniq("perfil")
    owner = f"perfil-{s}@p0.test"
    await _negocio(s, owner)
    h = await _headers(app_client, owner)

    me = (await app_client.get(ME, headers=h)).json()
    assert me["email"] == owner and me["nombre"] is None and me["telefono"] is None
    assert me["avatar_url"] is None

    r = await app_client.patch(
        ME,
        headers=h,
        json={"nombre": "Ana Pérez", "telefono": "+57 300 123 4567", "username": f"ana-{s}"},
    )
    assert r.status_code == 200, r.text
    actualizado = r.json()
    assert actualizado["nombre"] == "Ana Pérez"
    assert actualizado["telefono"] == "+57 300 123 4567"
    assert actualizado["username"] == f"ana-{s}"

    de_nuevo = (await app_client.get(ME, headers=h)).json()
    assert de_nuevo["nombre"] == "Ana Pérez" and de_nuevo["username"] == f"ana-{s}"


async def test_username_duplicado_y_formato(app_client) -> None:
    s = uniq("uname")
    owner = f"perfil-{s}@p0.test"
    await _negocio(s, owner)
    h = await _headers(app_client, owner)
    await _admin_con_username(f"otro-{s}@p0.test", f"tomado-{s}")

    r = await app_client.patch(ME, headers=h, json={"username": f"tomado-{s}"})
    assert r.status_code == 409, r.text

    r = await app_client.patch(ME, headers=h, json={"username": "ab"})
    assert r.status_code == 400, r.text


async def test_cambio_de_password(app_client) -> None:
    s = uniq("pass")
    owner = f"perfil-{s}@p0.test"
    await _negocio(s, owner)
    h = await _headers(app_client, owner)

    r = await app_client.post("/api/v1/admin/me/password", headers=h, json={"actual": "incorrecta", "nueva": "otra-clave-456"})
    assert r.status_code == 400, r.text

    r = await app_client.post("/api/v1/admin/me/password", headers=h, json={"actual": PW, "nueva": "otra-clave-456"})
    assert r.status_code == 204, r.text

    # La contraseña nueva sirve para entrar.
    login = await app_client.post("/api/v1/auth/login", json={"identifier": owner, "password": "otra-clave-456"})
    assert login.status_code == 200, login.text


async def test_avatar_subida_y_borrado(app_client) -> None:
    s = uniq("avatar")
    owner = f"perfil-{s}@p0.test"
    await _negocio(s, owner)
    h = await _headers(app_client, owner)

    r = await app_client.post(AVATAR, headers=h, files={"file": ("foto.png", PNG, "image/png")})
    assert r.status_code == 200, r.text
    subido = r.json()
    assert subido["avatar_url"]

    me = (await app_client.get(ME, headers=h)).json()
    assert me["avatar_url"] == subido["avatar_url"]

    # La foto se sirve por la API (mismo origen para el panel, sin CSP).
    img = await app_client.get("/api/v1/admin/me/avatar", headers=h)
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/")
    visible = await app_client.get(f"/api/v1/admin/collaborators/{me['admin_id']}/avatar", headers=h)
    assert visible.status_code == 200, visible.text

    r = await app_client.delete(AVATAR, headers=h)
    assert r.status_code == 204, r.text
    me = (await app_client.get(ME, headers=h)).json()
    assert me["avatar_url"] is None

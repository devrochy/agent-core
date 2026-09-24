"""API de campo del gestor — contrato de la APK (ADR-0065).

Router separado del `api.py` de campo v1 (ADR-0057). Implementa el contrato exacto
de `app-cobranza-apk`: mismas rutas, **camelCase** idéntico a `types/gestor.ts` y
**IDs como string** (UUID).

Identidad unificada (ADR-0066): gestores y propietarios son **colaboradores del
core** (`admins` + `tenant_user`); el gestor además tiene su perfil `Gestor`.

F1 (esta tanda): autenticación y perfil.
- `POST /auth/gestor/login` `{tenant, usuario, password}` → `{accessToken, refreshToken, gestor}`.
- `POST /auth/propietario/login` `{tenant, usuario, password}` → `{accessToken, refreshToken, propietario}`.
- `POST /auth/refresh` `{refreshToken}` → `{accessToken, refreshToken}`.
- `PATCH /perfil` `{nombre, apellido}` → `PerfilPublic`.

`tenant` (slug) es una adición obligatoria del contrato: agent-core es multi-tenant y
el login no traía identificador de tenant.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.admin.auth import verify_password
from core.db.models import Admin, TenantUser
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from pack_cobranza.auth import issue_token, pack_secret, token_tenant
from pack_cobranza.models import Gestor

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"
router = APIRouter(prefix="/api/pack/pack_cobranza", tags=[f"{PLUGIN_ID}_gestor"])

_ACCESS_TTL_H = 12
_REFRESH_TTL_H = 720  # 30 días


def _par_tokens(secret: str, tenant_id: uuid.UUID, admin_id: uuid.UUID, *, rol: str, gestor_id: uuid.UUID | None = None) -> dict[str, str]:
    base = {"tenant_id": str(tenant_id), "admin_id": str(admin_id), "rol": rol}
    if gestor_id is not None:
        base["gestor_id"] = str(gestor_id)
    return {
        "accessToken": issue_token({**base, "typ": "access"}, secret, ttl_hours=_ACCESS_TTL_H),
        "refreshToken": issue_token({**base, "typ": "refresh"}, secret, ttl_hours=_REFRESH_TTL_H),
    }


def _usuario_de(admin: Admin) -> str:
    return admin.username or admin.email


async def _auth(authorization: str | None) -> tuple[uuid.UUID, dict[str, Any]] | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return await token_tenant(authorization.split(" ", 1)[1].strip())


async def _admin_por_credenciales(session, usuario: str, password: str) -> Admin | None:
    admin = (
        await session.execute(
            select(Admin).where((Admin.email == usuario.lower()) | (Admin.username == usuario), Admin.active.is_(True))
        )
    ).scalar_one_or_none()
    if admin is None or not verify_password(password, admin.password_hash):
        return None
    return admin


@router.post("/auth/gestor/login")
async def gestor_login(request: Request) -> JSONResponse:
    body = await request.json()
    slug = str(body.get("tenant") or "").strip()
    usuario = str(body.get("usuario") or "").strip()
    password = str(body.get("password") or "")
    if not (slug and usuario and password):
        return JSONResponse({"error": "faltan datos"}, status_code=400)

    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return JSONResponse({"error": "tenant desconocido"}, status_code=401)

    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            admin = await _admin_por_credenciales(session, usuario, password)
            if admin is None:
                return JSONResponse({"error": "credenciales inválidas"}, status_code=401)
            if await _tenant_link(session, tenant_id, admin.id) is None:
                return JSONResponse({"error": "sin acceso a este tenant"}, status_code=403)
            gestor = (
                await session.execute(select(Gestor).where(Gestor.tenant_id == tenant_id, Gestor.admin_id == admin.id, Gestor.deleted_at.is_(None)))
            ).scalar_one_or_none()
            if gestor is None or gestor.estatus != "activo":
                return JSONResponse({"error": "no es gestor de esta cartera"}, status_code=403)
            tokens = _par_tokens(secret, tenant_id, admin.id, rol="gestor", gestor_id=gestor.id)
            perfil = {"id": str(gestor.id), "usuario": _usuario_de(admin), "nombre": gestor.nombre, "apellido": gestor.apellido or ""}
    return JSONResponse({**tokens, "gestor": perfil})


@router.post("/auth/propietario/login")
async def propietario_login(request: Request) -> JSONResponse:
    body = await request.json()
    slug = str(body.get("tenant") or "").strip()
    usuario = str(body.get("usuario") or "").strip()
    password = str(body.get("password") or "")
    if not (slug and usuario and password):
        return JSONResponse({"error": "faltan datos"}, status_code=400)

    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return JSONResponse({"error": "tenant desconocido"}, status_code=401)

    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            admin = await _admin_por_credenciales(session, usuario, password)
            if admin is None:
                return JSONResponse({"error": "credenciales inválidas"}, status_code=401)
            link = await _tenant_link(session, tenant_id, admin.id)
            if link is None or link.role != "owner":
                return JSONResponse({"error": "no es propietario de este tenant"}, status_code=403)
            tokens = _par_tokens(secret, tenant_id, admin.id, rol="propietario")
            perfil = {"id": str(admin.id), "usuario": _usuario_de(admin), "nombre": _usuario_de(admin), "apellido": ""}
    return JSONResponse({**tokens, "propietario": perfil})


async def _tenant_link(session, tenant_id: uuid.UUID, admin_id: uuid.UUID) -> TenantUser | None:
    return (
        await session.execute(select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id))
    ).scalar_one_or_none()


@router.post("/auth/refresh")
async def refresh(request: Request) -> JSONResponse:
    body = await request.json()
    token = str(body.get("refreshToken") or "")
    auth = await token_tenant(token)
    if auth is None:
        return JSONResponse({"error": "refresh inválido"}, status_code=401)
    tenant_id, payload = auth
    if payload.get("typ") != "refresh":
        return JSONResponse({"error": "token no es de refresco"}, status_code=401)
    admin_id = payload.get("admin_id")
    if not admin_id:
        return JSONResponse({"error": "token sin admin"}, status_code=401)
    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            admin = (await session.execute(select(Admin).where(Admin.id == uuid.UUID(str(admin_id)), Admin.active.is_(True)))).scalar_one_or_none()
            if admin is None:
                return JSONResponse({"error": "admin inactivo"}, status_code=401)
            gestor_id = uuid.UUID(str(payload["gestor_id"])) if payload.get("gestor_id") else None
            tokens = _par_tokens(secret, tenant_id, admin.id, rol=str(payload.get("rol") or "gestor"), gestor_id=gestor_id)
    return JSONResponse(tokens)


@router.patch("/perfil")
async def actualizar_perfil(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    body = await request.json()
    nombre = str(body.get("nombre") or "").strip()
    apellido = str(body.get("apellido") or "").strip()
    if not nombre:
        return JSONResponse({"error": "nombre requerido"}, status_code=400)
    gestor_id = payload.get("gestor_id")
    admin_id = payload.get("admin_id")
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if gestor_id:
                gestor = await get_owned(session, Gestor, uuid.UUID(str(gestor_id)))
                if gestor is None:
                    return JSONResponse({"error": "gestor no encontrado"}, status_code=404)
                gestor.nombre = nombre
                gestor.apellido = apellido or None
                perfil = {"id": str(gestor.id), "usuario": "", "nombre": gestor.nombre, "apellido": gestor.apellido or ""}
            elif admin_id:
                admin = (await session.execute(select(Admin).where(Admin.id == uuid.UUID(str(admin_id))))).scalar_one_or_none()
                if admin is None:
                    return JSONResponse({"error": "usuario no encontrado"}, status_code=404)
                perfil = {"id": str(admin.id), "usuario": _usuario_de(admin), "nombre": nombre, "apellido": apellido}
            else:
                return JSONResponse({"error": "token sin identidad"}, status_code=401)
    return JSONResponse(perfil)

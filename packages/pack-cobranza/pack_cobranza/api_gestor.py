"""API de campo del gestor — contrato de la APK (ADR-0065).

Router separado del `api.py` de campo v1 (ADR-0057). Implementa el contrato exacto
de `app-cobranza-apk`: mismas rutas, **camelCase** idéntico a `types/gestor.ts` y
**IDs como string** (UUID).

F1 (esta tanda): autenticación y perfil.
- `POST /auth/gestor/login` `{tenant, usuario, password}` → `{accessToken, refreshToken, gestor}`.
- `POST /auth/propietario/login` → 501 (decisión abierta: el propietario es el tenant, ADR-0062).
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


def _par_tokens(secret: str, gestor: Gestor) -> dict[str, str]:
    base = {"tenant_id": str(gestor.tenant_id), "gestor_id": str(gestor.id), "rol": "gestor"}
    return {
        "accessToken": issue_token({**base, "typ": "access"}, secret, ttl_hours=_ACCESS_TTL_H),
        "refreshToken": issue_token({**base, "typ": "refresh"}, secret, ttl_hours=_REFRESH_TTL_H),
    }


def _gestor_public(gestor: Gestor) -> dict[str, Any]:
    return {
        "id": str(gestor.id),
        "usuario": gestor.usuario or "",
        "nombre": gestor.nombre,
        "apellido": gestor.apellido or "",
    }


async def _auth(authorization: str | None) -> tuple[uuid.UUID, dict[str, Any]] | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return await token_tenant(authorization.split(" ", 1)[1].strip())


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
            gestor = (
                await session.execute(select(Gestor).where(Gestor.tenant_id == tenant_id, Gestor.usuario == usuario, Gestor.deleted_at.is_(None)))
            ).scalar_one_or_none()
            ok = gestor is not None and gestor.estatus == "activo" and bool(gestor.password_hash) and verify_password(password, gestor.password_hash or "")
            if not ok:
                return JSONResponse({"error": "credenciales inválidas"}, status_code=401)
            tokens = _par_tokens(secret, gestor)
            perfil = _gestor_public(gestor)
    return JSONResponse({**tokens, "gestor": perfil})


@router.post("/auth/propietario/login")
async def propietario_login() -> JSONResponse:
    # El propietario ES el tenant (ADR-0062): no hay usuario de propietario en el pack.
    # Decisión de identidad abierta (ADR-0065); v1 responde 501.
    return JSONResponse({"error": "login de propietario no soportado todavía"}, status_code=501)


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
    gestor_id = payload.get("gestor_id")
    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            gestor = await get_owned(session, Gestor, uuid.UUID(str(gestor_id))) if gestor_id else None
            if gestor is None or gestor.estatus != "activo":
                return JSONResponse({"error": "gestor inválido"}, status_code=401)
            tokens = _par_tokens(secret, gestor)
    return JSONResponse(tokens)


@router.patch("/perfil")
async def actualizar_perfil(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    gestor_id = payload.get("gestor_id")
    body = await request.json()
    nombre = str(body.get("nombre") or "").strip()
    apellido = str(body.get("apellido") or "").strip()
    if not nombre:
        return JSONResponse({"error": "nombre requerido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            gestor = await get_owned(session, Gestor, uuid.UUID(str(gestor_id))) if gestor_id else None
            if gestor is None:
                return JSONResponse({"error": "gestor no encontrado"}, status_code=404)
            gestor.nombre = nombre
            gestor.apellido = apellido or None
            perfil = _gestor_public(gestor)
    return JSONResponse(perfil)

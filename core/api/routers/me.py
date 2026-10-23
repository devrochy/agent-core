"""Identidad del request y perfil propio: `GET/PATCH /api/v1/admin/me`.

Sirve al panel y a las apps para resolver sesión, tenant activo y permisos tras
el login, sin adivinar el contrato. La lista COMPLETA de tenants no viaja aquí
(un master puede tener cientos): el selector usa `/api/v1/admin/tenants`.

El perfil (SPEC-34) añade `nombre`, `telefono` y `avatar_url` (foto del usuario). El avatar
se sube con `POST /me/avatar` al controlador único de almacenamiento (perfil `avatar`) y la
API devuelve una URL firmada de vida corta; se resuelve en cada lectura de `/me`.

`invitaciones_pendientes` (HU-SH-06): cuántas invitaciones esperan respuesta, para que el
panel las muestre al entrar (una cuenta que ya existía recibe la invitación en la app,
ADR-0121). La lista sale de `/api/v1/admin/invitations/pending`.
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from core.admin.auth import hash_password, verify_password
from core.admin.invitations_service import contar_pendientes
from core.admin.roles import testers_habilitados
from core.almacenamiento import StorageInvalido, abrir, borrar, guardar, resolver_avatar
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.api.schemas import TenantOption
from core.db.models import Admin
from core.db.session import get_session

router = APIRouter(prefix="/admin", tags=["admin · sesión"])

MAX_BYTES_AVATAR = 2 * 1024 * 1024
_USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,40}$")


class MeResponse(BaseModel):
    admin_id: uuid.UUID
    email: str
    username: str | None
    nombre: str | None = None
    telefono: str | None = None
    avatar_url: str | None = None
    role: str
    tenant: TenantOption | None
    tenant_count: int
    permissions: dict[str, str | None]
    invitaciones_pendientes: int
    qa_testers_habilitados: bool = False


class PerfilUpdate(BaseModel):
    nombre: str | None = None
    telefono: str | None = None
    username: str | None = None


class PasswordChange(BaseModel):
    actual: str
    nueva: str


async def _me_response(principal: Principal, admin: Admin) -> MeResponse:
    tenant = next(
        (
            TenantOption(id=tid, name=name, role=role, active=True)
            for (tid, name, role) in principal.tenants
            if tid == principal.tenant_id
        ),
        None,
    )
    return MeResponse(
        admin_id=admin.id,
        email=admin.email,
        username=admin.username,
        nombre=admin.nombre,
        telefono=admin.telefono,
        avatar_url=resolver_avatar(admin.avatar_url),
        role=admin.role,
        tenant=tenant,
        tenant_count=len(principal.tenants),
        permissions=principal.permissions,
        invitaciones_pendientes=await contar_pendientes(admin.id),
        # El panel muestra «Testers» y su aviso solo si el rol existe en este entorno (ADR-0134).
        qa_testers_habilitados=testers_habilitados(),
    )


@router.get("/me", response_model=MeResponse)
async def get_me(principal: Annotated[Principal, Depends(get_principal)]) -> MeResponse:
    return await _me_response(principal, principal.admin)


@router.patch("/me", response_model=MeResponse)
async def update_me(
    payload: PerfilUpdate,
    principal: Annotated[Principal, Depends(get_principal)],
) -> MeResponse:
    """Actualiza el perfil propio (nombre, teléfono y usuario único)."""
    async with get_session() as session:
        actual = await session.get(Admin, principal.admin.id)
        if actual is None:
            raise ApiError.not_found("Cuenta no encontrada.")
        if payload.nombre is not None:
            actual.nombre = payload.nombre.strip()[:120] or None
        if payload.telefono is not None:
            actual.telefono = payload.telefono.strip()[:40] or None
        if payload.username is not None:
            usuario = payload.username.strip()
            if usuario:
                if not _USERNAME.fullmatch(usuario):
                    raise ApiError.bad_request(
                        "El usuario debe tener entre 3 y 40 caracteres: letras, números, puntos, guiones o guion bajo.",
                        "username_invalido",
                    )
                ocupado = (
                    await session.execute(
                        select(Admin.id).where(Admin.username == usuario, Admin.id != actual.id)
                    )
                ).scalar_one_or_none()
                if ocupado is not None:
                    raise ApiError(409, "conflict", "Ese nombre de usuario ya está en uso.")
                actual.username = usuario
            else:
                actual.username = None
        await session.flush()
        return await _me_response(principal, actual)


@router.post("/me/password", status_code=204)
async def change_password(
    payload: PasswordChange,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    """Cambia la contraseña propia pidiendo la actual."""
    if len(payload.nueva) < 8:
        raise ApiError.bad_request("La contraseña nueva debe tener al menos 8 caracteres.", "password_corta")
    async with get_session() as session:
        actual = await session.get(Admin, principal.admin.id)
        if actual is None:
            raise ApiError.not_found("Cuenta no encontrada.")
        if not verify_password(payload.actual, actual.password_hash):
            raise ApiError.bad_request("La contraseña actual no es correcta.", "password_actual")
        actual.password_hash = hash_password(payload.nueva)
        await session.flush()
    return Response(status_code=204)


@router.post("/me/avatar", response_model=MeResponse)
async def upload_avatar(
    principal: Annotated[Principal, Depends(get_principal)],
    file: Annotated[UploadFile, File()],
) -> MeResponse:
    """Sube (o reemplaza) la foto de perfil del usuario."""
    contenido = await file.read(MAX_BYTES_AVATAR + 1)
    if len(contenido) > MAX_BYTES_AVATAR:
        raise ApiError.bad_request("La imagen no puede superar 2 MB.", "avatar_grande")
    try:
        ref = guardar("avatar", contenido, mimetype=file.content_type, nombre_base=str(principal.admin.id))
    except StorageInvalido as exc:
        raise ApiError.bad_request(str(exc), exc.codigo) from exc
    async with get_session() as session:
        actual = await session.get(Admin, principal.admin.id)
        if actual is None:
            raise ApiError.not_found("Cuenta no encontrada.")
        previo = actual.avatar_url
        actual.avatar_url = ref.key
        await session.flush()
        anterior = previo if previo and not previo.startswith(("http://", "https://")) else None
    if anterior and anterior != ref.key:
        borrar(anterior)
    return await _me_response(principal, actual)


@router.delete("/me/avatar", status_code=204)
async def delete_avatar(principal: Annotated[Principal, Depends(get_principal)]) -> Response:
    """Quita la foto de perfil del usuario."""
    async with get_session() as session:
        actual = await session.get(Admin, principal.admin.id)
        if actual is None:
            raise ApiError.not_found("Cuenta no encontrada.")
        previo = actual.avatar_url
        actual.avatar_url = None
        await session.flush()
    if previo and not previo.startswith(("http://", "https://")):
        borrar(previo)
    return Response(status_code=204)


@router.get("/me/avatar")
async def get_avatar(principal: Annotated[Principal, Depends(get_principal)]) -> FileResponse:
    """Sirve la foto del usuario autenticado (mismo origen para el panel, sin CSP)."""
    valor = principal.admin.avatar_url
    if not valor or valor.startswith(("http://", "https://")):
        raise ApiError.not_found("No tienes foto de perfil.")
    ruta = abrir(valor)
    if ruta is None:
        raise ApiError.not_found("La foto no está disponible.")
    media = mimetypes.guess_type(ruta.name)[0] or "application/octet-stream"
    return FileResponse(ruta, media_type=media, headers={"Cache-Control": "private, max-age=60"})

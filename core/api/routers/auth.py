"""Autenticación de la API: `POST /api/v1/auth/login` y `/logout`.

Login devuelve el token opaco de `AdminSession` (misma sesión revocable que el
panel, ADR-0077). El cliente web lo guarda en una cookie `httpOnly`; las apps lo
guardan en almacenamiento seguro y lo envían como `Authorization: Bearer`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from core.admin.auth import SESSION_TTL_HOURS, admin_from_token, login, logout
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    identifier: str = Field(description="Correo o nombre de usuario del admin.")
    password: str


class LoginResponse(BaseModel):
    token: str
    admin_id: uuid.UUID
    expires_in: int = Field(description="Segundos de vigencia de la sesión.")


@router.post("/login", response_model=LoginResponse)
async def post_login(payload: LoginRequest) -> LoginResponse:
    token = await login(payload.identifier, payload.password)
    if token is None:
        raise ApiError(401, "invalid_credentials", "Correo o contraseña incorrectos.")
    admin = await admin_from_token(token)
    if admin is None:  # credenciales válidas pero admin desactivado
        raise ApiError.unauthorized("La cuenta no está activa.")
    return LoginResponse(token=token, admin_id=admin.id, expires_in=SESSION_TTL_HOURS * 3600)


@router.post("/logout", status_code=204)
async def post_logout(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    if principal.token:
        await logout(principal.token)
    return Response(status_code=204)

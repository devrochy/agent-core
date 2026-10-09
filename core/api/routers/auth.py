"""Autenticación de la API: `POST /api/v1/auth/login`, `/logout`,
`/forgot-password` y `/reset-password`.

Login devuelve el token opaco de `AdminSession` (misma sesión revocable que el
panel, ADR-0077). El cliente web lo guarda en una cookie `httpOnly`; las apps lo
guardan en almacenamiento seguro y lo envían como `Authorization: Bearer`.

La recuperación de contraseña (ADR-0098) usa un código de un solo uso enviado
por el correo de sistema; `forgot-password` responde siempre 202 sin filtrar si
la cuenta existe.

`accept-invitation` (ADR-0120) canjea el enlace de un solo uso con el que el dueño
de un negocio creado por el alta automática define su contraseña y entra.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.background import BackgroundTasks
from pydantic import BaseModel, Field

from core.admin.auth import SESSION_TTL_HOURS, admin_from_token, login, logout
from core.admin.invitations_service import CUENTA_CON_CONTRASENA, canjear_invitacion
from core.admin.password_reset_service import aplicar_reset, solicitar_reset
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


class ForgotPasswordRequest(BaseModel):
    identifier: str = Field(description="Correo o nombre de usuario.")


class ResetPasswordRequest(BaseModel):
    token: str = Field(description="Código de recuperación recibido por correo.")
    new_password: str = Field(min_length=8)


class AcceptInvitationRequest(BaseModel):
    token: str = Field(
        max_length=200,
        description="Token del enlace de la invitación: lo que va después de `#` en `/invitacion#…`.",
    )
    new_password: str = Field(min_length=8, description="Contraseña nueva de la cuenta (8+).")


@router.post("/login", response_model=LoginResponse)
async def post_login(payload: LoginRequest, request: Request) -> LoginResponse:
    from core.security.login_lockout import (
        client_ip,
        is_blocked,
        register_failure,
        register_success,
    )

    ip = client_ip(request)
    identifier = payload.identifier.strip().lower()
    if await is_blocked("admin", identifier, ip):
        raise ApiError(
            429, "auth_bloqueado", "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."
        )
    token = await login(payload.identifier, payload.password)
    if token is None:
        await register_failure("admin", identifier, ip)
        raise ApiError(401, "invalid_credentials", "Correo o contraseña incorrectos.")
    await register_success("admin", identifier, ip)
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


@router.post("/forgot-password", status_code=202)
async def post_forgot_password(payload: ForgotPasswordRequest, background: BackgroundTasks) -> Response:
    """Siempre 202 con el mismo cuerpo: no filtra si la cuenta existe (ADR-0098).

    El trabajo pesado (hash + correo) va en background para emparejar el tiempo
    de respuesta de cuentas existentes e inexistentes.
    """
    background.add_task(solicitar_reset, payload.identifier)
    return Response(status_code=202)


@router.post("/reset-password", status_code=204)
async def post_reset_password(payload: ResetPasswordRequest) -> Response:
    error = await aplicar_reset(payload.token, payload.new_password)
    if error is not None:
        raise ApiError(400, "invalid_token", error)
    return Response(status_code=204)


@router.post("/accept-invitation", status_code=204)
async def post_accept_invitation(payload: AcceptInvitationRequest) -> Response:
    """Canjea el enlace de un solo uso de una invitación (ADR-0120): define la contraseña de
    una cuenta creada sin ella y la deja como miembro del negocio. Luego se entra con
    `/auth/login`. Un enlace inválido, usado o vencido da 400 sin decir cuál de los tres;
    una cuenta que ya tiene contraseña da 409 (entra y acepta la invitación en la app)."""
    error = await canjear_invitacion(payload.token, payload.new_password)
    if error == CUENTA_CON_CONTRASENA:
        raise ApiError(409, "account_has_password", error)
    if error is not None:
        raise ApiError(400, "invalid_token", error)
    return Response(status_code=204)

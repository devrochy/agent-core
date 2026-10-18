"""Autenticación de la API: `POST /api/v1/auth/login`, `/logout`,
`/forgot-password` y `/reset-password`.

Login devuelve el token opaco de `AdminSession` (misma sesión revocable que el
panel, ADR-0077). El cliente web lo guarda en una cookie `httpOnly`; las apps lo
guardan en almacenamiento seguro y lo envían como `Authorization: Bearer`.

La recuperación de contraseña (ADR-0098) usa un código de un solo uso enviado
por el correo de sistema; `forgot-password` responde siempre 202 sin filtrar si
la cuenta existe.

`accept-invitation` (ADR-0121) canjea el enlace de un solo uso con el que el dueño
de un negocio creado por el alta automática define su contraseña y entra: devuelve la
sesión, igual que el login (ADR-0124).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response
from fastapi.background import BackgroundTasks
from pydantic import BaseModel, Field

from core.admin.auth import (
    SESSION_TTL_HOURS,
    NegocioSuspendido,
    admin_from_token,
    login,
    logout,
)
from core.admin.invitations_service import (
    CUENTA_EXISTENTE,
    ENLACE_INVALIDO,
    canjear_invitacion,
    partes_del_token,
)
from core.admin.password_reset_service import aplicar_reset, solicitar_reset
from core.api.deps import session_token
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
    identifier: str | None = Field(
        default=None,
        max_length=320,
        description="Correo o usuario de la cuenta: el código solo se busca entre los suyos.",
    )


class AcceptInvitationRequest(BaseModel):
    token: str = Field(
        max_length=200,
        description="Token del enlace de la invitación: lo que va después de `#` en `/invitacion#…`.",
    )
    new_password: str = Field(min_length=8, description="Contraseña nueva de la cuenta (8+).")


class AcceptInvitationResponse(BaseModel):
    """Sesión del dueño que acaba de definir su contraseña (ADR-0124), como la del login."""

    token: str
    admin_id: uuid.UUID
    tenant_id: uuid.UUID = Field(description="Negocio al que entró: el panel lo deja activo.")
    expires_in: int = Field(description="Segundos de vigencia de la sesión.")


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
    try:
        token = await login(payload.identifier, payload.password)
    except NegocioSuspendido as exc:
        # Credenciales correctas: no cuenta como intento fallido (ADR-0136).
        raise ApiError(403, exc.code, exc.mensaje) from None
    if token is None:
        await register_failure("admin", identifier, ip)
        raise ApiError(401, "invalid_credentials", "Correo o contraseña incorrectos.")
    await register_success("admin", identifier, ip)
    admin = await admin_from_token(token)
    if admin is None:  # credenciales válidas pero admin desactivado
        raise ApiError.unauthorized("La cuenta no está activa.")
    return LoginResponse(token=token, admin_id=admin.id, expires_in=SESSION_TTL_HOURS * 3600)


@router.post("/logout", status_code=204)
async def post_logout(request: Request) -> Response:
    """Cierra la sesión del token presentado. No resuelve negocio ni permisos: quien quedó
    con un negocio suspendido (403 en el resto de la API) también puede salir (ADR-0136)."""
    token = session_token(request)
    if not token:
        raise ApiError.unauthorized()
    await logout(token)
    return Response(status_code=204)


# Cupo de pedidos de código (HU-SH-06): cada pedido anula el código anterior y manda un correo.
# Sin límite, alguien podría llenar de correos a una persona y no dejarla recuperar su cuenta.
FORGOT_POR_IDENTIFICADOR = 3
FORGOT_POR_IP = 10
FORGOT_VENTANA_S = 900


@router.post("/forgot-password", status_code=202)
async def post_forgot_password(
    payload: ForgotPasswordRequest, request: Request, background: BackgroundTasks
) -> Response:
    """Siempre 202 con el mismo cuerpo: no filtra si la cuenta existe (ADR-0098).

    El trabajo pesado (hash + correo) va en background para emparejar el tiempo
    de respuesta de cuentas existentes e inexistentes. Con un cupo por identificador
    (3 cada 15 min) y por IP (10): pasado el cupo responde igual, pero no genera ni
    envía otro código.
    """
    from core.security.login_lockout import client_ip, within_quota

    identifier = payload.identifier.strip().lower()
    if await within_quota(
        "reset_pedido",
        identifier or None,
        client_ip(request),
        por_identificador=FORGOT_POR_IDENTIFICADOR,
        por_ip=FORGOT_POR_IP,
        ventana_s=FORGOT_VENTANA_S,
    ):
        background.add_task(solicitar_reset, payload.identifier)
    return Response(status_code=202)


@router.post("/reset-password", status_code=204)
async def post_reset_password(payload: ResetPasswordRequest, request: Request) -> Response:
    """Aplica el código. Con el mismo bloqueo progresivo del login (5 fallos en 10 min → 15
    min), por IP y por identificador si viene (HU-SH-06)."""
    from core.security.login_lockout import (
        client_ip,
        is_blocked,
        register_failure,
        register_success,
    )

    ip = client_ip(request)
    identifier = (payload.identifier or "").strip().lower() or None
    if await is_blocked("reset", identifier, ip):
        raise ApiError(
            429, "auth_bloqueado", "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."
        )
    error = await aplicar_reset(payload.token, payload.new_password, identifier)
    if error is not None:
        await register_failure("reset", identifier, ip)
        raise ApiError(400, "invalid_token", error)
    await register_success("reset", identifier, ip)
    return Response(status_code=204)


@router.post("/accept-invitation", response_model=AcceptInvitationResponse)
async def post_accept_invitation(payload: AcceptInvitationRequest, request: Request) -> AcceptInvitationResponse:
    """Canjea el enlace de un solo uso de una invitación (ADR-0121): define la contraseña de
    una cuenta que el alta automática creó sin ella, la deja como miembro del negocio y abre
    su sesión (ADR-0124), así el dueño entra directo. Un enlace inválido, usado o vencido da
    400 sin decir cuál de los tres; una cuenta que ya existe (con contraseña, acceso a otro
    negocio u otra invitación pendiente) da 409: entra y acepta en la app. Con el mismo
    bloqueo progresivo del login (5 fallos en 10 min → 15 min), por IP y por invitación."""
    from core.security.login_lockout import (
        client_ip,
        is_blocked,
        register_failure,
        register_success,
    )

    ip = client_ip(request)
    partes = partes_del_token(payload.token)
    identificador = str(partes[0]) if partes else None
    if await is_blocked("invitacion", identificador, ip):
        raise ApiError(
            429, "auth_bloqueado", "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."
        )
    error, hecho = await canjear_invitacion(payload.token, payload.new_password)
    if error is None and hecho is not None:
        await register_success("invitacion", identificador, ip)
        return AcceptInvitationResponse(
            token=hecho.token_de_sesion,
            admin_id=hecho.admin_id,
            tenant_id=hecho.tenant_id,
            expires_in=SESSION_TTL_HOURS * 3600,
        )
    await register_failure("invitacion", identificador, ip)
    if error == CUENTA_EXISTENTE:
        raise ApiError(409, "account_exists", error)
    raise ApiError(400, "invalid_token", error or ENLACE_INVALIDO)


class AcceptTesterInvitationRequest(BaseModel):
    token: str = Field(max_length=200, description="Lo que va después de `#` en `/invitacion-tester#…`.")
    new_password: str = Field(min_length=8, description="Contraseña nueva del tester (8+).")


class AcceptTesterInvitationResponse(BaseModel):
    token: str
    admin_id: uuid.UUID
    expires_in: int = Field(description="Segundos de vigencia de la sesión.")


@router.post("/accept-tester-invitation", response_model=AcceptTesterInvitationResponse)
async def post_accept_tester_invitation(
    payload: AcceptTesterInvitationRequest, request: Request
) -> AcceptTesterInvitationResponse:
    """Canjea el enlace de un tester (SPEC-30, ADR-0134): define su contraseña y abre su sesión.
    Inválido, usado, vencido o con el rol apagado: 400 sin decir cuál. Mismo bloqueo
    progresivo del login, por IP y por invitación."""
    from core.qa.testers import ENLACE_INVALIDO, canjear_invitacion
    from core.security.login_lockout import (
        client_ip,
        is_blocked,
        register_failure,
        register_success,
    )

    ip = client_ip(request)
    partes = partes_del_token(payload.token)
    identificador = f"tester:{partes[0]}" if partes else None
    if await is_blocked("invitacion", identificador, ip):
        raise ApiError(
            429, "auth_bloqueado", "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."
        )
    error, token, admin_id = await canjear_invitacion(payload.token, payload.new_password)
    if error is None and token is not None and admin_id is not None:
        await register_success("invitacion", identificador, ip)
        return AcceptTesterInvitationResponse(token=token, admin_id=admin_id, expires_in=SESSION_TTL_HOURS * 3600)
    await register_failure("invitacion", identificador, ip)
    raise ApiError(400, "invalid_token", error or ENLACE_INVALIDO)

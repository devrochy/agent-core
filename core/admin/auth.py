"""Autenticación del panel: sesiones con cookie + bcrypt.

Decisiones (ADR-025 de agent-sale):
- Sesión en BD (no JWT): revocación inmediata — cada request valida admins.active.
- Cookie httponly + samesite=lax. En producción agregar secure=True (HTTPS).
- La misma sesión sirve por `Authorization: Bearer` para clientes no-navegador
  (API `/api/v1`, apps): ADR-0077. Sigue siendo revocable al desactivar el admin.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import Request
from sqlalchemy import select

from core.admin.roles import cuenta_habilitada
from core.db.models import Admin, AdminSession
from core.db.session import get_session

SESSION_COOKIE = "agent_core_session"


class NegocioSuspendido(Exception):
    """Credenciales válidas, pero todos los negocios de la cuenta están suspendidos
    (ADR-0136). La API responde 403 `tenant_suspended`; el panel clásico, 403 con el
    mismo texto. No se crea sesión."""

    code = "tenant_suspended"
    mensaje = "El negocio está suspendido. Contacta al administrador de la plataforma."

    def __init__(self) -> None:
        super().__init__(self.mensaje)

TENANT_COOKIE = "ac_tenant"
SESSION_TTL_HOURS = 24 * 7


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


async def login(email_or_username: str, password: str) -> str | None:
    """Devuelve el token de sesión o None si las credenciales son inválidas.

    Lanza `NegocioSuspendido` (sin crear sesión) cuando la contraseña es correcta pero la
    cuenta solo tiene negocios suspendidos (ADR-0136). Se comprueba DESPUÉS de la
    contraseña: una credencial inválida responde igual exista o no el negocio. Una cuenta
    sin negocios (invitada y sin aceptar) entra como siempre.
    """
    from core.shared.tenant import negocios_de_cuenta

    async with get_session() as session:
        admin = (
            await session.execute(
                select(Admin).where(
                    (Admin.email == email_or_username) | (Admin.username == email_or_username),
                    Admin.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if admin is None or not verify_password(password, admin.password_hash):
            return None
        if not cuenta_habilitada(admin):
            # Tester con el rol apagado (producción, ADR-0134): no entra.
            return None
        admin_id = admin.id
    if (await negocios_de_cuenta(admin)).solo_suspendidos:
        raise NegocioSuspendido()
    async with get_session() as session:
        return nueva_sesion(session, admin_id)


def nueva_sesion(session, admin_id: uuid.UUID) -> str:  # type: ignore[no-untyped-def]
    """Agrega una `AdminSession` nueva en la transacción `session` y devuelve su token. Solo
    después de probar la identidad: la contraseña (`login`) o el enlace de un solo uso de la
    invitación recién canjeado (ADR-0124)."""
    token = secrets.token_urlsafe(48)
    session.add(
        AdminSession(
            token=token,
            admin_id=admin_id,
            expires_at=datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS),
        )
    )
    return token


async def logout(token: str) -> None:
    async with get_session() as session:
        stored = await session.get(AdminSession, token)
        if stored is not None:
            await session.delete(stored)


async def revocar_sesiones(session, admin_id: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    """Borra TODAS las sesiones de un admin dentro de la transacción `session` y devuelve
    cuántas había. Va junto al cambio que las invalida (desactivar la cuenta, cambiarle la
    contraseña desde Colaboradores, renovar una cuenta de prueba), en la misma transacción:
    así una cuenta reactivada no resucita sesiones viejas y el siguiente request del
    panel o de la app responde 401 aunque el token siga en el cliente."""
    from sqlalchemy import delete

    result = await session.execute(delete(AdminSession).where(AdminSession.admin_id == admin_id))
    return int(result.rowcount or 0)


def bearer_token(request: Request) -> str | None:
    """Token de `Authorization: Bearer <token>`, o None. No valida la sesión."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def admin_from_token(token: str) -> Admin | None:
    """Resuelve el admin dueño de una sesión. Valida expiración Y admins.active.

    Misma lógica que la cookie: la revocación es inmediata (borrar/desactivar la
    sesión o el admin invalida el token en el siguiente request).
    """
    async with get_session() as session:
        stored = await session.get(AdminSession, token)
        if stored is None or stored.expires_at < datetime.now(UTC):
            return None
        admin = await session.get(Admin, stored.admin_id)
        if not cuenta_habilitada(admin):
            return None
        return admin


async def current_admin(request: Request) -> Admin | None:
    """Resuelve el admin desde la cookie de sesión o, si falta, un Bearer."""
    token = request.cookies.get(SESSION_COOKIE) or bearer_token(request)
    if not token:
        return None
    return await admin_from_token(token)

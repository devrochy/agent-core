"""Autenticación del panel: sesiones con cookie + bcrypt.

Decisiones (ADR-025 de agent-sale):
- Sesión en BD (no JWT): revocación inmediata — cada request valida admins.active.
- Cookie httponly + samesite=lax. En producción agregar secure=True (HTTPS).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import Request
from sqlalchemy import select

from core.db.models import Admin, AdminSession
from core.db.session import get_session

SESSION_COOKIE = "agent_core_session"
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
    """Devuelve el token de sesión o None si las credenciales son inválidas."""
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
        token = secrets.token_urlsafe(48)
        session.add(
            AdminSession(
                token=token,
                admin_id=admin.id,
                expires_at=datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS),
            )
        )
        return token


async def logout(token: str) -> None:
    async with get_session() as session:
        stored = await session.get(AdminSession, token)
        if stored is not None:
            await session.delete(stored)


async def current_admin(request: Request) -> Admin | None:
    """Resuelve el admin de la cookie. Valida expiración Y admins.active (revocación inmediata)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    async with get_session() as session:
        stored = await session.get(AdminSession, token)
        if stored is None or stored.expires_at < datetime.now(UTC):
            return None
        admin = await session.get(Admin, stored.admin_id)
        if admin is None or not admin.active:
            return None
        return admin

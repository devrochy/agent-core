"""Recuperación de contraseña self-service con código de un solo uso (ADR-0094).

- `solicitar_reset`: respuesta siempre genérica; si el admin existe y está
  activo, invalida tokens previos sin usar, genera un código de 8 caracteres de
  un alfabeto sin ambigüedades (~40 bits), guarda su hash con expiración de 15
  minutos y lo envía por el correo de sistema (log-only sin SMTP configurado).
- `aplicar_reset`: valida el código contra el hash (compare_digest), su
  expiración y uso único; aplica la contraseña nueva y revoca TODAS las
  sesiones del admin.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from core.admin.auth import hash_password
from core.db.models import Admin, AdminSession, PasswordResetToken
from core.db.session import get_session
from core.gateway.system_email import send_system_email
from core.shared.logger import get_logger

log = get_logger(__name__)

TTL_MINUTES = 15
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sin 0/O, 1/I/L
_CODE_LENGTH = 8


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _generar_codigo() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


async def solicitar_reset(identifier: str) -> None:
    """Solicita un código de recuperación. Nunca revela si la cuenta existe."""
    ident = (identifier or "").strip().lower()
    async with get_session() as session:
        admin = (
            await session.execute(
                select(Admin).where(
                    (Admin.email == ident) | (Admin.username == ident),
                    Admin.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if admin is None:
            return

        # Limpia expirados (acota el escaneo de `aplicar_reset`) e invalida los
        # códigos previos sin usar: solo el último código pedido vale.
        await session.execute(
            delete(PasswordResetToken).where(
                (PasswordResetToken.expires_at < datetime.now(UTC))
                | (
                    (PasswordResetToken.admin_id == admin.id)
                    & PasswordResetToken.used_at.is_(None)
                )
            )
        )

        code = _generar_codigo()
        session.add(
            PasswordResetToken(
                admin_id=admin.id,
                token_hash=_code_hash(code),
                expires_at=datetime.now(UTC) + timedelta(minutes=TTL_MINUTES),
            )
        )
        destinatario = admin.email or ident
        await session.commit()

    subject = "Tu código de recuperación de SolversHub"
    body = (
        f"Recibimos una solicitud para restablecer tu contraseña.\n\n"
        f"Código: {code}\n\n"
        f"Vence en {TTL_MINUTES} minutos. Si no fuiste tú, ignora este correo."
    )
    try:
        enviado = await send_system_email(destinatario, subject, body)
    except Exception:
        log.warning("password_reset_email_failed", admin_id=str(admin.id), exc_info=True)
        return
    log.info(
        "password_reset_requested",
        admin_id=str(admin.id),
        enviado=bool(enviado),
    )


async def aplicar_reset(token: str, new_password: str) -> str | None:
    """Aplica el código y la contraseña nueva. Devuelve un error (str) o None."""
    code = (token or "").strip().upper()
    if len(code) != _CODE_LENGTH or any(c not in _CODE_ALPHABET for c in code):
        return "código inválido"

    if not new_password or len(new_password) < 8:
        return "la contraseña debe tener al menos 8 caracteres"

    wanted = _code_hash(code)
    async with get_session() as session:
        filas = (
            await session.execute(
                select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None))
            )
        ).scalars().all()
        now = datetime.now(UTC)
        valido = None
        for t in filas:
            if hmac.compare_digest(t.token_hash.encode(), wanted.encode()):
                if t.expires_at > now:
                    valido = t
                break
        if valido is None:
            return "código inválido o vencido"

        admin = await session.get(Admin, valido.admin_id)
        if admin is None or not admin.active:
            return "código inválido o vencido"

        # Uso único atómico: el UPDATE condicional cierra la ventana TOCTOU de
        # dos requests concurrentes con el mismo código.
        filas_afectadas = await session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.id == valido.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=datetime.now(UTC))
        )
        if filas_afectadas.rowcount != 1:
            return "código inválido o vencido"

        admin.password_hash = hash_password(new_password)
        await session.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
        await session.commit()
    log.info("password_reset_applied", admin_id=str(admin.id))
    return None

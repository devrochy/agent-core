"""Testers del centro de pruebas (SPEC-30, HU-QA-04, ADR-0134).

Un tester es una cuenta de plataforma (`admins.role = 'tester'`) que actúa como master
salvo en Colaboradores, y solo en un entorno con `QA_TESTERS_ENABLED` (test). Lo invita
el master: la cuenta nace sin contraseña y recibe un enlace de un solo uso
(`<id>.<HMAC(APP_SECRET_KEY, id + nonce)>`, en el `#` de la URL) con el que define su
contraseña y entra. Revocar desactiva la cuenta y borra sus sesiones: el siguiente
pedido ya falla (la sesión se valida en cada request).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update

from core.admin.invitations_service import SIN_CONTRASENA, partes_del_token, url_del_panel
from core.admin.roles import ROL_TESTER, testers_habilitados
from core.db.models import Admin, AdminSession, QaTesterInvitation
from core.db.session import get_session
from core.qa.service import QaError
from core.shared.logger import get_logger

log = get_logger(__name__)

VIGENCIA = timedelta(days=7)
ENLACE_INVALIDO = "el enlace no es válido o ya venció"
_CONTEXTO = "qa-tester-invitation:v1"
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _firma(invitation_id: uuid.UUID, nonce: str) -> str:
    from core.config import get_settings

    clave = get_settings().app_secret_key.encode()
    digest = hmac.new(clave, f"{_CONTEXTO}:{invitation_id}:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def enlace(invitation_id: uuid.UUID, nonce: str) -> str:
    """El token va en el fragmento: el navegador no lo manda al servidor ni queda en logs."""
    return f"{url_del_panel()}/invitacion-tester#{invitation_id}.{_firma(invitation_id, nonce)}"


def _exigir_habilitado() -> None:
    if not testers_habilitados():
        raise QaError("conflicto", "El rol tester no está habilitado en este entorno (QA_TESTERS_ENABLED).")


def _estado(admin: Admin, invitacion: QaTesterInvitation | None, ahora: datetime) -> str:
    if not admin.active:
        return "revocado"
    if admin.password_hash != SIN_CONTRASENA:
        return "activo"
    if invitacion is not None and invitacion.estado == "pendiente" and invitacion.expires_at > ahora:
        return "invitado"
    return "vencido"


def _a_dict(admin: Admin, invitacion: QaTesterInvitation | None, ahora: datetime) -> dict[str, Any]:
    return {
        "id": str(admin.id),
        "email": admin.email,
        "estado": _estado(admin, invitacion, ahora),
        "creado_en": admin.created_at.isoformat() if admin.created_at else None,
        "invitado_en": invitacion.creada_en.isoformat() if invitacion else None,
        "expira_en": invitacion.expires_at.isoformat() if invitacion else None,
        "aceptado_en": (
            invitacion.respondida_en.isoformat()
            if invitacion and invitacion.estado == "aceptada" and invitacion.respondida_en
            else None
        ),
    }


async def _ultima_invitacion(session, admin_id: uuid.UUID) -> QaTesterInvitation | None:  # type: ignore[no-untyped-def]
    return (
        await session.execute(
            select(QaTesterInvitation)
            .where(QaTesterInvitation.admin_id == admin_id)
            .order_by(QaTesterInvitation.creada_en.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def listar_testers() -> list[dict[str, Any]]:
    ahora = datetime.now(UTC)
    async with get_session() as session:
        testers = (
            await session.execute(select(Admin).where(Admin.role == ROL_TESTER).order_by(Admin.created_at.desc()))
        ).scalars().all()
        return [_a_dict(t, await _ultima_invitacion(session, t.id), ahora) for t in testers]


async def invitar_tester(actor: Admin, email: str) -> tuple[dict[str, Any], str]:
    """Invita (o reinvita) a un tester. Devuelve el tester y el enlace (el master lo puede
    compartir a mano si el correo de sistema no está configurado)."""
    _exigir_habilitado()
    correo = (email or "").strip().lower()
    if not _EMAIL_RE.fullmatch(correo) or len(correo) > 254:
        raise QaError("invalido", "Correo inválido.")
    ahora = datetime.now(UTC)
    nonce = secrets.token_urlsafe(16)
    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == correo))).scalar_one_or_none()
        if admin is not None and admin.role != ROL_TESTER:
            raise QaError(
                "conflicto",
                "Ya existe una cuenta con ese correo. Un tester usa una cuenta propia, solo para pruebas.",
            )
        if admin is None:
            admin = Admin(email=correo, password_hash=SIN_CONTRASENA, role=ROL_TESTER, active=True)
            session.add(admin)
            await session.flush()
        else:
            # Reinvitar: la cuenta vuelve a definir su contraseña con el enlace nuevo.
            admin.active = True
            admin.password_hash = SIN_CONTRASENA
            await session.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
            await session.execute(
                update(QaTesterInvitation)
                .where(QaTesterInvitation.admin_id == admin.id, QaTesterInvitation.estado == "pendiente")
                .values(estado="reemplazada", token_nonce=None, respondida_en=ahora)
            )
        invitacion = QaTesterInvitation(
            admin_id=admin.id,
            email=correo,
            token_nonce=nonce,
            estado="pendiente",
            expires_at=ahora + VIGENCIA,
            invitado_por=actor.id,
        )
        session.add(invitacion)
        await session.flush()
        salida = _a_dict(admin, invitacion, ahora)
        url = enlace(invitacion.id, nonce)
    await _enviar_correo(correo, url)
    log.info("qa_tester_invitado", tester_id=salida["id"])
    return salida, url


async def _enviar_correo(correo: str, url: str) -> None:
    from core.gateway.system_email import send_system_email

    cuerpo = (
        "Te invitaron a probar SolversHub en el entorno de pruebas.\n\n"
        f"Define tu contraseña y entra con este enlace (vence en {VIGENCIA.days} días, se usa una vez):\n"
        f"{url}\n\n"
        "Es un entorno de pruebas: los negocios y los datos son de prueba.\n"
    )
    try:
        await send_system_email(correo, "Invitación para probar SolversHub", cuerpo)
    except Exception as exc:  # el enlace igual vuelve al master para compartirlo a mano
        log.warning("qa_tester_correo_fallo", error=type(exc).__name__)


async def revocar_tester(admin_id: uuid.UUID) -> dict[str, Any]:
    ahora = datetime.now(UTC)
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None or admin.role != ROL_TESTER:
            raise QaError("no_encontrado", "Tester no encontrado.")
        admin.active = False
        await session.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
        await session.execute(
            update(QaTesterInvitation)
            .where(QaTesterInvitation.admin_id == admin.id, QaTesterInvitation.estado == "pendiente")
            .values(estado="revocada", token_nonce=None, respondida_en=ahora)
        )
        await session.flush()
        salida = _a_dict(admin, await _ultima_invitacion(session, admin.id), ahora)
    log.info("qa_tester_revocado", tester_id=str(admin_id))
    return salida


async def canjear_invitacion(token: str, new_password: str) -> tuple[str | None, str | None, uuid.UUID | None]:
    """Define la contraseña del tester y abre su sesión. `(error, token_de_sesion, admin_id)`.

    Uso único atómico: la contraseña se escribe solo si la cuenta sigue sin una y la
    invitación se acepta con un UPDATE condicional sobre su nonce."""
    from core.admin.auth import hash_password, nueva_sesion

    if not testers_habilitados():
        return ENLACE_INVALIDO, None, None
    partes = partes_del_token(token)
    if partes is None:
        return ENLACE_INVALIDO, None, None
    if not new_password or len(new_password) < 8:
        return "la contraseña debe tener al menos 8 caracteres", None, None
    invitation_id, firma = partes
    ahora = datetime.now(UTC)
    async with get_session() as session:
        invitacion = await session.get(QaTesterInvitation, invitation_id)
        if invitacion is None or invitacion.estado != "pendiente" or not invitacion.token_nonce:
            return ENLACE_INVALIDO, None, None
        nonce = invitacion.token_nonce
        if not hmac.compare_digest(firma.encode(), _firma(invitacion.id, nonce).encode()):
            return ENLACE_INVALIDO, None, None
        if invitacion.expires_at <= ahora:
            return ENLACE_INVALIDO, None, None
        admin = await session.get(Admin, invitacion.admin_id)
        if admin is None or not admin.active or admin.role != ROL_TESTER:
            return ENLACE_INVALIDO, None, None
        cambio = await session.execute(
            update(Admin)
            .where(Admin.id == admin.id, Admin.password_hash == SIN_CONTRASENA)
            .values(password_hash=hash_password(new_password))
        )
        if getattr(cambio, "rowcount", 0) != 1:
            await session.rollback()
            return ENLACE_INVALIDO, None, None
        aceptada = await session.execute(
            update(QaTesterInvitation)
            .where(
                QaTesterInvitation.id == invitacion.id,
                QaTesterInvitation.estado == "pendiente",
                QaTesterInvitation.token_nonce == nonce,
            )
            .values(estado="aceptada", token_nonce=None, respondida_en=ahora)
        )
        if getattr(aceptada, "rowcount", 0) != 1:
            await session.rollback()
            return ENLACE_INVALIDO, None, None
        admin_id = admin.id
        token_de_sesion = nueva_sesion(session, admin_id)
    log.info("qa_tester_invitacion_aceptada", tester_id=str(admin_id))
    return None, token_de_sesion, admin_id

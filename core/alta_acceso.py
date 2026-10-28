"""Acceso del dueño cuando el master da de alta un negocio desde el panel (ADR-0175).

El panel ya no pide una contraseña para el dueño: la cuenta nace **sin contraseña** y le llega
un correo con un enlace de un solo uso para definirla y entrar (el mismo mecanismo de
invitación y canje del aprovisionamiento automático, ADR-0121). Si la cuenta ya existe o ya
entra a otro negocio, el correo no lleva enlace: avisa y manda al panel a aceptar la
invitación (ADR-0081).

Es una pieza del **alta**, como `core.alta_negocio`: solo la usa la API de plataforma del
master. Las tools y el LLM no llegan acá (`.importlinter`, `tests/test_provisioning_sin_llm.py`).
El correo sale **después** de confirmar la invitación, nunca dentro de una transacción; si no
sale, el negocio queda creado y se reenvía a mano (`reenviar_acceso`).
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.models import Admin, Tenant, TenantInvitation
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.texto_seguro import contiene_url, enmascarar_correo

log = get_logger(__name__)

VIGENCIA_ENLACE = timedelta(days=7)
# Tope de espera del SMTP: `asyncio.wait_for` corta la espera del endpoint (no el hilo del envío).
TIMEOUT_ENVIO_S = 10.0

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Estado = Literal["enviado", "sin_smtp", "fallido", "ya_emitida", "ya_es_miembro"]
Tipo = Literal["enlace", "app"]


def correo_valido(valor: Any) -> str | None:
    texto = str(valor or "").strip().lower()
    if not texto or len(texto) > 254 or not _EMAIL.match(texto):
        return None
    return texto


@dataclass(frozen=True)
class InvitacionOwner:
    invitation_id: uuid.UUID
    admin_id: uuid.UUID
    email: str
    con_enlace: bool
    # El enlace lleva el token: fuera del `repr` para que no salga en un log ni en un traceback.
    enlace: str = field(repr=False)
    vence: datetime | None

    @property
    def vence_texto(self) -> str:
        return self.vence.strftime("%d/%m/%Y") if self.vence else ""


def asunto_y_cuerpo(
    inv: InvitacionOwner, *, negocio: str, nombre: str | None, origen: Literal["compra", "panel"] = "compra"
) -> tuple[str, str]:
    """El asunto no lleva el nombre del negocio (lo escribe quien lo crea: sirve para
    phishing); el cuerpo sí, ya validado sin enlaces. `origen` solo cambia el texto de la
    invitación en la app: una compra no es lo mismo que un alta hecha por el equipo."""
    saludo = f"Hola {nombre}," if nombre else "Hola,"
    if inv.con_enlace:
        asunto = "Tu acceso a SolversHub"
        cuerpo = (
            f"{saludo}\n\n"
            f"Tu negocio «{negocio}» ya está creado en SolversHub.\n\n"
            "Para entrar, define tu contraseña con este enlace "
            f"(es de un solo uso y vence el {inv.vence_texto}):\n{inv.enlace}\n\n"
            "Si no fuiste tú, ignora este correo: sin este enlace nadie puede entrar con tu correo."
        )
    elif origen == "panel":
        asunto = "Te dieron acceso a un negocio en SolversHub"
        cuerpo = (
            f"{saludo}\n\n"
            f"Te dieron acceso como dueño del negocio «{negocio}» en SolversHub.\n"
            f"Entra con tu cuenta ({inv.email}) y acepta la invitación:\n{inv.enlace}\n\n"
            "Si no esperabas este acceso, no lo aceptes."
        )
    else:
        asunto = "Te invitaron a un negocio en SolversHub"
        cuerpo = (
            f"{saludo}\n\n"
            f"Te invitaron como dueño del negocio «{negocio}» en SolversHub.\n"
            f"Entra con tu cuenta ({inv.email}) y acepta la invitación:\n{inv.enlace}\n\n"
            "Si no reconoces esta compra, no la aceptes."
        )
    return asunto, cuerpo


async def enviar_acceso(
    inv: InvitacionOwner, *, negocio: str, nombre: str | None, origen: Literal["compra", "panel"] = "compra"
) -> str:
    """`enviado` | `sin_smtp` (correo de sistema sin configurar, ADR-0098). Un fallo del SMTP
    sube (`OSError`/`SMTPException`): quien llama decide si reintenta o lo informa."""
    from core.gateway.system_email import send_system_email

    asunto, cuerpo = asunto_y_cuerpo(inv, negocio=negocio, nombre=nombre, origen=origen)
    enviado = await send_system_email(inv.email, asunto, cuerpo)
    return "enviado" if enviado else "sin_smtp"


# ------------------------------------------------------------------ alta desde el panel
class AccesoRechazado(Exception):
    """No se puede dar ese acceso (`codigo` estable para la API, `mensaje` para quien opera)."""

    def __init__(self, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


@dataclass(frozen=True)
class AccesoEmitido:
    estado: Estado
    tipo: Tipo
    expires_at: datetime | None


def _nombre_para_correo(negocio: str) -> str:
    """El nombre del negocio va en el cuerpo: sin saltos de línea ni enlaces."""
    limpio = " ".join(str(negocio or "").split())[:80]
    return "tu negocio" if not limpio or contiene_url(limpio) else limpio


async def motivo_para_no_dar_acceso(email: str) -> str | None:
    """Antes de crear nada: el correo no puede ser de una cuenta desactivada, de un master, de
    un tester ni de una cuenta con permisos globales (ADR-0081). Así no queda un negocio sin
    dueño ni un enlace que no se pueda canjear."""
    from core.admin.invitations_service import motivo_para_no_invitar

    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()
        if admin is None:
            return None
        admin_id, activa = admin.id, admin.active
    if not activa:
        return "la cuenta de ese correo está desactivada"
    return await motivo_para_no_invitar(admin_id)


async def asegurar_cuenta_sin_contrasena(email: str) -> tuple[uuid.UUID, bool]:
    """La cuenta del correo: (id, creada). Si no existe se crea **sin contraseña** (el enlace
    la define). Si otra petición la crea a la vez, para esta ya existía."""
    from core.admin.invitations_service import SIN_CONTRASENA

    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()
        if admin is not None:
            return admin.id, False
    try:
        async with get_session() as session:
            admin = Admin(email=email, password_hash=SIN_CONTRASENA, role="colaborador", active=True)
            session.add(admin)
            await session.flush()
            admin_id = admin.id
        log.info("alta_acceso_cuenta_creada", admin_id=str(admin_id))
        return admin_id, True
    except IntegrityError:
        async with get_session() as session:
            admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one()
            return admin.id, False


def _armar(inv: TenantInvitation, email: str) -> InvitacionOwner:
    from core.admin.invitations_service import (
        enlace_de_invitacion,
        token_de_invitacion,
        url_del_panel,
    )

    con_enlace = bool(inv.token_nonce)
    enlace = (
        enlace_de_invitacion(token_de_invitacion(inv.id, inv.token_nonce))
        if con_enlace and inv.token_nonce
        else url_del_panel()
    )
    return InvitacionOwner(
        invitation_id=inv.id,
        admin_id=inv.admin_id,
        email=email,
        con_enlace=con_enlace,
        enlace=enlace,
        vence=inv.expires_at,
    )


async def _entregar(inv: InvitacionOwner, negocio: str, tenant_id: uuid.UUID) -> Estado:
    """Envía el correo con tope de espera. Un fallo no tumba el alta: se informa."""
    inicio = time.monotonic()
    try:
        estado = await asyncio.wait_for(
            enviar_acceso(inv, negocio=_nombre_para_correo(negocio), nombre=None, origen="panel"),
            TIMEOUT_ENVIO_S,
        )
    except (OSError, smtplib.SMTPException, TimeoutError) as exc:
        log.warning(
            "tenant_owner_access_failed",
            tenant_id=str(tenant_id),
            invitation_id=str(inv.invitation_id),
            correo=enmascarar_correo(inv.email),
            error=type(exc).__name__,
            duracion_envio_ms=int((time.monotonic() - inicio) * 1000),
        )
        return "fallido"
    registrar = log.warning if estado == "sin_smtp" else log.info
    registrar(
        "tenant_owner_access_sent",
        tenant_id=str(tenant_id),
        invitation_id=str(inv.invitation_id),
        correo=enmascarar_correo(inv.email),
        estado=estado,
        tipo="enlace" if inv.con_enlace else "app",
        duracion_envio_ms=int((time.monotonic() - inicio) * 1000),
    )
    return estado  # type: ignore[return-value]


async def _auditar(
    tenant_id: uuid.UUID, actor_email: str, acceso: AccesoEmitido, invitation_id: uuid.UUID, email: str, *, reenvio: bool
) -> None:
    from core.shared.audit import record_audit
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        await record_audit(
            actor=f"admin:{actor_email}",
            action="tenant.owner_access",
            output_data={
                "invitation_id": str(invitation_id),
                "estado": acceso.estado,
                "tipo": acceso.tipo,
                "reenvio": reenvio,
                "correo": enmascarar_correo(email),
            },
        )


async def emitir_y_enviar_acceso(
    *, tenant_id: uuid.UUID, admin_id: uuid.UUID, email: str, negocio: str, actor_email: str
) -> AccesoEmitido:
    """Invitación `owner` del negocio recién creado y el correo con el acceso.

    Idempotente: si ya había una pendiente (un doble POST) no emite ni envía otra
    (`ya_emitida`); el reenvío es explícito. Con enlace solo si la cuenta está limpia."""
    from core.admin.invitations_service import YA_PERTENECE, cuenta_limpia, emitir_invitacion_owner

    limpia = await cuenta_limpia(admin_id, tenant_id)
    invitacion, nuevo, error = await emitir_invitacion_owner(
        tenant_id=tenant_id, admin_id=admin_id, con_enlace=limpia, vigencia=VIGENCIA_ENLACE
    )
    if error == YA_PERTENECE:
        return AccesoEmitido("ya_es_miembro", "app", None)
    if error is not None or invitacion is None:
        raise AccesoRechazado("owner_email_rejected", f"no se pudo invitar al dueño: {error}")
    tipo: Tipo = "enlace" if invitacion.token_nonce else "app"
    if not nuevo:
        return AccesoEmitido("ya_emitida", tipo, invitacion.expires_at)
    inv = _armar(invitacion, email)
    estado = await _entregar(inv, negocio, tenant_id)
    acceso = AccesoEmitido(estado, tipo, invitacion.expires_at)
    await _auditar(tenant_id, actor_email, acceso, invitacion.id, email, reenvio=False)
    return acceso


async def reenviar_acceso(*, tenant_id: uuid.UUID, actor_email: str) -> AccesoEmitido:
    """Reenvía el acceso al dueño de un negocio (panel, master/tester). Solo a la cuenta de la
    invitación `owner` pendiente de ese negocio: el destinatario no se recibe de la petición."""
    from core.admin.invitations_service import renovar_enlace_owner

    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None or tenant.deleted_at is not None:
            raise AccesoRechazado("tenant_no_encontrado", "Negocio no encontrado.")
        if tenant.status != "active":
            raise AccesoRechazado("tenant_inactivo", "El negocio no está activo: actívalo primero.")
        negocio = tenant.name
    invitacion, _con_enlace, error = await renovar_enlace_owner(tenant_id=tenant_id, vigencia=VIGENCIA_ENLACE)
    if error is not None or invitacion is None:
        log.info("tenant_owner_access_resend_rejected", tenant_id=str(tenant_id), motivo=error)
        raise AccesoRechazado("sin_invitacion_pendiente", error or "No hay una invitación pendiente.")
    async with get_session() as session:
        admin = await session.get(Admin, invitacion.admin_id)
        email = admin.email if admin is not None else ""
    log.info("tenant_owner_access_resend", tenant_id=str(tenant_id), invitation_id=str(invitacion.id))
    inv = _armar(invitacion, email)
    estado = await _entregar(inv, negocio, tenant_id)
    acceso = AccesoEmitido(estado, "enlace" if inv.con_enlace else "app", invitacion.expires_at)
    await _auditar(tenant_id, actor_email, acceso, invitacion.id, email, reenvio=True)
    return acceso


__all__ = [
    "TIMEOUT_ENVIO_S",
    "VIGENCIA_ENLACE",
    "AccesoEmitido",
    "AccesoRechazado",
    "InvitacionOwner",
    "asegurar_cuenta_sin_contrasena",
    "asunto_y_cuerpo",
    "correo_valido",
    "emitir_y_enviar_acceso",
    "enviar_acceso",
    "motivo_para_no_dar_acceso",
    "reenviar_acceso",
]

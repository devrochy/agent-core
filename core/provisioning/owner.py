"""Owner del negocio nuevo: cuenta, invitación y entrega del enlace (HU-SH-03, ADR-0121 § 3.8).

- Correo sin cuenta → `Admin` sin contraseña (`SIN_CONTRASENA`) e invitación `owner` con
  enlace de un solo uso: al abrirlo, el owner define su contraseña (D-SPEC-07-2).
- Correo de una cuenta existente → invitación pendiente de siempre (ADR-0081): entra con
  su contraseña y la acepta en la app. No se toca su contraseña.
- Entrega por correo de sistema (ADR-0098) y por WhatsApp en la conversación del comprador
  con el vendedor (aviso por evento `provisioning.invitation`, ADR-0108).

Corre bajo el `tenant_scope` del vendedor: la conversación de WhatsApp es la suya.
"""

from __future__ import annotations

import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.models import Admin, Conversation
from core.db.session import get_session
from core.gateway.event_notices import clean_param
from core.provisioning import AltaEnRevision, AltaTransitoria
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

VIGENCIA_ENLACE = timedelta(days=7)
EVENTO_INVITACION = "provisioning.invitation"

# Textos de respaldo del aviso por WhatsApp (dentro de la ventana de 24 h). El negocio
# vendedor los puede reemplazar en Plantillas por evento (`fallback_text`, ADR-0108).
TEXTO_WHATSAPP_CON_ENLACE = (
    "¡Listo, {cliente.nombre}! Tu negocio «{negocio.nombre}» ya está creado. Para entrar, "
    "define tu contraseña con este enlace (es de un solo uso y vence el {invitacion.vence}): "
    "{invitacion.enlace}"
)
TEXTO_WHATSAPP_EN_APP = (
    "¡Listo, {cliente.nombre}! Tu negocio «{negocio.nombre}» ya está creado. Entra con tu "
    "cuenta ({invitacion.correo}) y acepta la invitación: {invitacion.enlace}"
)


@dataclass(frozen=True)
class InvitacionOwner:
    invitation_id: uuid.UUID
    admin_id: uuid.UUID
    email: str
    con_enlace: bool
    enlace: str
    vence: datetime | None

    @property
    def vence_texto(self) -> str:
        return self.vence.strftime("%d/%m/%Y") if self.vence else ""


# ------------------------------------------------------------------ cuenta e invitación
async def validar_correo_del_owner(email: str) -> None:
    """Antes de crear nada: el correo no puede ser de una cuenta desactivada, de un master ni
    de una cuenta con permisos globales (ADR-0081). Si lo es, el alta queda en revisión sin
    dejar un negocio sin dueño."""
    from core.admin.invitations_service import motivo_para_no_invitar

    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()
    if admin is None:
        return
    if not admin.active:
        raise AltaEnRevision("la cuenta de ese correo está desactivada")
    motivo = await motivo_para_no_invitar(admin.id)
    if motivo is not None:
        raise AltaEnRevision(f"no se puede invitar a ese correo: {motivo}")


async def _cuenta_del_owner(email: str) -> Admin:
    """La cuenta del correo; si no existe, la crea sin contraseña."""
    from core.admin.invitations_service import SIN_CONTRASENA

    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()
    if admin is not None:
        return admin
    try:
        async with get_session() as session:
            admin = Admin(email=email, password_hash=SIN_CONTRASENA, role="colaborador", active=True)
            session.add(admin)
            await session.flush()
        log.info("provisioning_owner_creado", admin_id=str(admin.id))
        return admin
    except IntegrityError:
        # Otra alta creó la misma cuenta a la vez (mismo correo en dos pedidos).
        async with get_session() as session:
            return (await session.execute(select(Admin).where(Admin.email == email))).scalar_one()


async def preparar_invitacion(tenant_id: uuid.UUID, email: str) -> InvitacionOwner | None:
    """Cuenta del owner + invitación `owner` al negocio. Idempotente.

    None si el owner ya es miembro (un reintento después de que aceptó). Lanza
    `AltaEnRevision` si el correo no se puede invitar (master, permisos globales o cuenta
    desactivada)."""
    from core.admin.invitations_service import (
        SIN_CONTRASENA,
        YA_PERTENECE,
        crear_invitacion_owner,
        enlace_de_invitacion,
        token_de_invitacion,
        url_del_panel,
    )

    admin = await _cuenta_del_owner(email)
    if not admin.active:
        raise AltaEnRevision("la cuenta de ese correo está desactivada")
    con_enlace = admin.password_hash == SIN_CONTRASENA
    invitacion, error = await crear_invitacion_owner(
        tenant_id=tenant_id, admin_id=admin.id, con_enlace=con_enlace, vigencia=VIGENCIA_ENLACE
    )
    if error == YA_PERTENECE:
        return None
    if error is not None or invitacion is None:
        raise AltaEnRevision(f"no se pudo invitar al dueño: {error}")
    if con_enlace and invitacion.token_nonce:
        enlace = enlace_de_invitacion(token_de_invitacion(invitacion.id, invitacion.token_nonce))
    else:
        enlace = url_del_panel()
    return InvitacionOwner(
        invitation_id=invitacion.id,
        admin_id=admin.id,
        email=email,
        con_enlace=bool(con_enlace and invitacion.token_nonce),
        enlace=enlace,
        vence=invitacion.expires_at,
    )


# ------------------------------------------------------------------ entrega
def asunto_y_cuerpo(inv: InvitacionOwner, *, negocio: str, nombre: str | None) -> tuple[str, str]:
    saludo = f"Hola {nombre}," if nombre else "Hola,"
    asunto = f"Tu negocio {negocio} ya está en SolversHub"
    if inv.con_enlace:
        cuerpo = (
            f"{saludo}\n\n"
            f"Tu negocio «{negocio}» ya está creado en SolversHub.\n\n"
            "Para entrar, define tu contraseña con este enlace "
            f"(es de un solo uso y vence el {inv.vence_texto}):\n{inv.enlace}\n\n"
            "Si no fuiste tú, ignora este correo."
        )
    else:
        cuerpo = (
            f"{saludo}\n\n"
            f"Tu negocio «{negocio}» ya está creado en SolversHub y te invitamos como dueño.\n"
            f"Entra con tu cuenta ({inv.email}) y acepta la invitación:\n{inv.enlace}\n\n"
            "Si no fuiste tú, ignora este correo."
        )
    return asunto, cuerpo


async def entregar_por_correo(inv: InvitacionOwner, *, negocio: str, nombre: str | None) -> str:
    """`enviado` | `sin_smtp` (correo de sistema sin configurar, ADR-0098). Un fallo del
    SMTP es transitorio: el outbox reintenta y este canal se vuelve a intentar."""
    from core.gateway.system_email import send_system_email

    asunto, cuerpo = asunto_y_cuerpo(inv, negocio=negocio, nombre=nombre)
    try:
        enviado = await send_system_email(inv.email, asunto, cuerpo)
    except (OSError, smtplib.SMTPException) as exc:
        raise AltaTransitoria(f"el correo de la invitación no salió ({type(exc).__name__})") from exc
    return "enviado" if enviado else "sin_smtp"


def contexto_del_aviso(inv: InvitacionOwner, *, negocio: str, nombre: str | None) -> dict[str, Any]:
    """Contexto del aviso: las variables salen de acá y solo de las rutas permitidas del
    evento (ADR-0108), nunca del texto del LLM."""
    return {
        "cliente": {"nombre": clean_param(nombre or "", 60) or "cliente"},
        "negocio": {"nombre": clean_param(negocio, 80)},
        "invitacion": {"enlace": inv.enlace, "vence": inv.vence_texto, "correo": inv.email},
    }


async def _conversacion_whatsapp(customer_id: uuid.UUID) -> Conversation | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == get_current_tenant_id(),
                    Conversation.customer_id == customer_id,
                    Conversation.channel == "whatsapp",
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def entregar_por_whatsapp(
    inv: InvitacionOwner, *, customer_id: uuid.UUID | None, negocio: str, nombre: str | None
) -> str:
    """Aviso en la conversación de WhatsApp del comprador con el vendedor. Devuelve el modo
    (`texto`, `plantilla`, `sin_plantilla`, `duplicado`, `no_enviado`) o `sin_conversacion`."""
    from core.gateway.event_notices import NoticeNotSentError, send_event_notice

    if customer_id is None:
        return "sin_conversacion"
    conversacion = await _conversacion_whatsapp(customer_id)
    if conversacion is None:
        return "sin_conversacion"
    try:
        resultado = await send_event_notice(
            conversacion.id,
            EVENTO_INVITACION,
            contexto_del_aviso(inv, negocio=negocio, nombre=nombre),
            notice_key=f"provisioning_invite:{inv.invitation_id}",
            default_fallback_text=TEXTO_WHATSAPP_CON_ENLACE if inv.con_enlace else TEXTO_WHATSAPP_EN_APP,
        )
    except NoticeNotSentError as exc:
        raise AltaTransitoria(f"el aviso por WhatsApp no salió: {exc}") from exc
    return resultado.modo


__all__ = [
    "EVENTO_INVITACION",
    "TEXTO_WHATSAPP_CON_ENLACE",
    "TEXTO_WHATSAPP_EN_APP",
    "VIGENCIA_ENLACE",
    "InvitacionOwner",
    "asunto_y_cuerpo",
    "contexto_del_aviso",
    "entregar_por_correo",
    "entregar_por_whatsapp",
    "preparar_invitacion",
    "validar_correo_del_owner",
]

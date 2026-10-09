"""Owner del negocio nuevo: cuenta, invitación, correo con el enlace y aviso por WhatsApp
(HU-SH-03, ADR-0121 § 3.8).

Reglas (endurecidas en la revisión de seguridad del PR #345):

- El enlace que define la contraseña va **solo por correo**, y solo si la cuenta la creó
  **esta** alta: queda anotado en `detalle.owner` de la fila del alta, en la misma
  transacción que crea la cuenta, para que un reintento lo sepa. Una cuenta que ya existía
  (con o sin contraseña) recibe la invitación en la app (ADR-0081), sin enlace.
- Por WhatsApp sale **siempre el mismo aviso**, sin token: el acceso llega al correo, que se
  muestra enmascarado. Es igual tenga o no cuenta, sea de un master o no, y aunque el alta
  quede en revisión: el comprador no puede averiguar nada de una cuenta con él.

Corre bajo el `tenant_scope` del vendedor: la fila del alta y la conversación son suyas.
"""

from __future__ import annotations

import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.models import Admin, Conversation, ProvisioningRequest
from core.db.session import get_owned, get_session
from core.gateway.event_notices import clean_param
from core.provisioning import AltaEnRevision, AltaTransitoria
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.shared.texto_seguro import enmascarar_correo

log = get_logger(__name__)

VIGENCIA_ENLACE = timedelta(days=7)
EVENTO_INVITACION = "provisioning.invitation"

# Aviso por WhatsApp (dentro de la ventana de 24 h). El negocio vendedor lo puede
# reemplazar en Plantillas por evento (`fallback_text`, ADR-0108); las variables del evento
# no incluyen ningún enlace ni token.
TEXTO_WHATSAPP = (
    "¡Gracias, {cliente.nombre}! El acceso a tu negocio «{negocio.nombre}» llega a tu correo "
    "({invitacion.correo}). Si en unos minutos no lo ves, revisa la carpeta de spam o "
    "escríbenos por aquí."
)


@dataclass(frozen=True)
class CuentaDelOwner:
    admin_id: uuid.UUID
    creada_en_esta_alta: bool


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


async def _anotar_owner(
    session: Any, vendedor: uuid.UUID, registro_id: uuid.UUID, admin_id: uuid.UUID, creada: bool
) -> None:
    fila = await get_owned(session, ProvisioningRequest, registro_id, vendedor)
    if fila is None:
        raise AltaTransitoria(f"la fila del alta {registro_id} no es visible")
    fila.detalle = {**(fila.detalle or {}), "owner": {"admin_id": str(admin_id), "creada": creada}}


async def cuenta_del_owner(
    email: str, *, vendedor: uuid.UUID, registro_id: uuid.UUID, previo: dict[str, Any] | None
) -> CuentaDelOwner:
    """La cuenta del correo y si la creó esta alta.

    Si no existe, se crea sin contraseña y se anota en la fila del alta en la MISMA
    transacción. Un reintento usa lo anotado (`previo`): para él la cuenta ya existe, pero
    la creó esta alta."""
    from core.admin.invitations_service import SIN_CONTRASENA

    if previo and previo.get("admin_id"):
        return CuentaDelOwner(uuid.UUID(str(previo["admin_id"])), bool(previo.get("creada")))
    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()
        if admin is not None:
            await _anotar_owner(session, vendedor, registro_id, admin.id, False)
            return CuentaDelOwner(admin.id, False)
    try:
        async with get_session() as session:
            admin = Admin(email=email, password_hash=SIN_CONTRASENA, role="colaborador", active=True)
            session.add(admin)
            await session.flush()
            await _anotar_owner(session, vendedor, registro_id, admin.id, True)
        log.info("provisioning_owner_creado", admin_id=str(admin.id))
        return CuentaDelOwner(admin.id, True)
    except IntegrityError:
        # Otra alta creó la misma cuenta a la vez: para esta, la cuenta ya existía.
        async with get_session() as session:
            admin = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one()
            await _anotar_owner(session, vendedor, registro_id, admin.id, False)
        return CuentaDelOwner(admin.id, False)


async def preparar_invitacion(
    tenant_id: uuid.UUID, email: str, cuenta: CuentaDelOwner
) -> InvitacionOwner | None:
    """Invitación `owner` al negocio. Idempotente. Lleva enlace solo si la cuenta la creó
    esta alta y sigue sin contraseña.

    None si el owner ya es miembro (un reintento después de que aceptó). Lanza
    `AltaEnRevision` si la cuenta no se puede invitar (master, permisos globales o
    desactivada)."""
    from core.admin.invitations_service import (
        SIN_CONTRASENA,
        YA_PERTENECE,
        crear_invitacion_owner,
        enlace_de_invitacion,
        token_de_invitacion,
        url_del_panel,
    )

    async with get_session() as session:
        admin = await session.get(Admin, cuenta.admin_id)
    if admin is None or not admin.active:
        raise AltaEnRevision("la cuenta de ese correo no está disponible")
    con_enlace = cuenta.creada_en_esta_alta and admin.password_hash == SIN_CONTRASENA
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
    """El asunto no lleva el nombre del negocio (lo escribe el comprador: sirve para
    phishing); el cuerpo sí, ya validado sin enlaces."""
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
    else:
        asunto = "Te invitaron a un negocio en SolversHub"
        cuerpo = (
            f"{saludo}\n\n"
            f"Te invitaron como dueño del negocio «{negocio}» en SolversHub.\n"
            f"Entra con tu cuenta ({inv.email}) y acepta la invitación:\n{inv.enlace}\n\n"
            "Si no reconoces esta compra, no la aceptes."
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


def contexto_del_aviso(*, negocio: str, nombre: str | None, email: str) -> dict[str, Any]:
    """Contexto del aviso por WhatsApp: sin enlace ni token, y el correo enmascarado. Las
    variables salen de acá y solo de las rutas permitidas del evento (ADR-0108)."""
    return {
        "cliente": {"nombre": clean_param(nombre or "", 60) or "cliente"},
        "negocio": {"nombre": clean_param(negocio, 80)},
        "invitacion": {"correo": enmascarar_correo(email)},
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


async def avisar_por_whatsapp(
    customer_id: uuid.UUID | None, *, negocio: str, nombre: str | None, email: str, aviso_key: str
) -> str:
    """El mismo aviso en todos los casos (sin enumeración). Best-effort: el acceso va por
    correo, así que un aviso que no sale no frena el alta. Devuelve el modo de
    `send_event_notice` o `sin_conversacion`."""
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
            contexto_del_aviso(negocio=negocio, nombre=nombre, email=email),
            notice_key=aviso_key,
            default_fallback_text=TEXTO_WHATSAPP,
        )
    except NoticeNotSentError:
        log.warning("provisioning_aviso_whatsapp_sin_enviar", conversation_id=str(conversacion.id))
        return "no_enviado"
    return resultado.modo


__all__ = [
    "EVENTO_INVITACION",
    "TEXTO_WHATSAPP",
    "VIGENCIA_ENLACE",
    "CuentaDelOwner",
    "InvitacionOwner",
    "asunto_y_cuerpo",
    "avisar_por_whatsapp",
    "contexto_del_aviso",
    "cuenta_del_owner",
    "entregar_por_correo",
    "preparar_invitacion",
    "validar_correo_del_owner",
]

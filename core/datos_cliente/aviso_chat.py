"""El aviso de datos en el chat y la respuesta del cliente (HU-CRM-16, ADR-0179 decisión 8).

- `texto_a_anexar`: la primera respuesta útil de una conversación lleva el aviso al final, UNA vez por
  versión; lo anexa el sistema (nunca el modelo) y deja la fila `informado`. No va en el canal de prueba.
- `interpretar` y `responder`: la tool `responder_aviso_datos` solo procede si el último aviso mostrado a
  esta persona es de la versión vigente, y solo toma la respuesta del último mensaje que el cliente
  escribió (leído de la base). Una respuesta que no es clara («ok», «dale») no cambia nada.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select

from core import consentimiento
from core.datos_cliente import aviso as aviso_mod
from core.db.models import AppSettings, Conversation, Customer, Message
from core.db.session import get_session
from core.memory import facts
from core.shared.logger import get_logger
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

# Solo respuestas inequívocas. «ok», «dale», «listo» NO bastan: no dicen qué se aceptó.
_ACEPTA = re.compile(
    r"^(acepto|si acepto|acepto los terminos|si autorizo|autorizo|estoy de acuerdo|de acuerdo|si estoy de acuerdo)$"
)
_RECHAZA = re.compile(r"^(no|no acepto|rechazo|no autorizo|no quiero|no estoy de acuerdo|no gracias)$")


def _normalizar(texto: str) -> str:
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", "", sin_tildes.lower()).strip()


def interpretar(texto: str) -> str | None:
    """«acepto» / «rechazo» si el mensaje entero lo dice con claridad; si no, None."""
    limpio = re.sub(r"\s+", " ", _normalizar(texto))
    if _ACEPTA.match(limpio):
        return "acepto"
    if _RECHAZA.match(limpio):
        return "rechazo"
    return None


async def _behavior(session: Any, tenant_id: uuid.UUID) -> dict[str, Any] | None:
    return (
        await session.execute(
            select(AppSettings.behavior_config).where(AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def aviso_activo(tenant_id: uuid.UUID | None) -> bool:
    if tenant_id is None:
        return False
    async with get_session() as session:
        return aviso_mod.desde(await _behavior(session, tenant_id)).activo


async def texto_a_anexar(
    tenant_id: uuid.UUID | None, conversation: Conversation, customer: Customer, behavior: dict[str, Any] | None
) -> str | None:
    """El aviso a anexar a esta respuesta, o None. Deja la fila `informado` si lo devuelve.

    `behavior` es el `behavior_config` que el loop ya cargó: con el aviso apagado (lo normal) no cuesta
    ninguna consulta. Bajo el candado por cliente (`facts._bloquear_cliente`) dos mensajes en ráfaga no
    avisan dos veces."""
    if tenant_id is None or conversation.channel == TEST_CHANNEL or customer.channel == TEST_CHANNEL:
        return None
    aviso = aviso_mod.desde(behavior)
    if not aviso.activo:
        return None
    try:
        async with get_session() as session:
            if await facts._bloquear_cliente(session, tenant_id, customer.id) is None:
                return None
            if await consentimiento.estado_aviso(session, tenant_id, customer.id, aviso.version) != consentimiento.SIN_AVISO:
                return None
            await consentimiento.registrar(
                session,
                tenant_id=tenant_id,
                customer_id=customer.id,
                estado=consentimiento.INFORMADO,
                version=aviso.version,
                origen=consentimiento.ORIGEN_CHAT,
                evidencia=f"conversacion:{conversation.id}",
            )
            return aviso.texto
    except Exception:
        # El aviso es una obligación del negocio, no una compuerta del turno: si falla, la respuesta sale.
        log.warning("aviso_datos_no_anexado", exc_info=True)
        return None


async def responder(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, customer_id: uuid.UUID, decision: str
) -> dict[str, Any]:
    """Registra la respuesta del cliente al aviso. `decision` es lo que el modelo entendió; el core la
    contrasta con el último mensaje real del cliente."""
    if decision not in ("acepto", "rechazo"):
        return {"error": "decision debe ser 'acepto' o 'rechazo'."}
    async with get_session() as session:
        aviso = aviso_mod.desde(await _behavior(session, tenant_id))
        if not aviso.activo:
            return {"error": "El negocio no tiene activo el aviso de datos."}
        if await facts._bloquear_cliente(session, tenant_id, customer_id) is None:
            return {"error": "Cliente no encontrado."}
        fila = await consentimiento.ultima_fila(session, tenant_id, customer_id)
        if (
            fila is None
            or fila.estado != consentimiento.INFORMADO
            or fila.version_aviso != aviso.version
            or fila.evidencia != f"conversacion:{conversation_id}"
        ):
            return {"error": "No hay un aviso pendiente de respuesta en esta conversación."}
        ultimo = (
            await session.execute(
                select(Message.content, Message.id)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).first()
        texto = ultimo[0] if ultimo else ""
        if interpretar(texto) != decision:
            return {
                "status": "sin_respuesta_clara",
                "hint": "El cliente no respondió con claridad. Pídele que escriba «acepto» o «no». No registres nada.",
            }
        estado = consentimiento.OTORGADO if decision == "acepto" else consentimiento.REVOCADO
        await consentimiento.registrar(
            session,
            tenant_id=tenant_id,
            customer_id=customer_id,
            estado=estado,
            version=aviso.version,
            origen=consentimiento.ORIGEN_CHAT,
            evidencia=f"mensaje:{ultimo[1]} «{texto[:200]}»",
        )
    return {"status": "registrado", "estado": estado}

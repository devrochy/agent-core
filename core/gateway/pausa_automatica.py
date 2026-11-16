"""La pausa del negocio y del cliente frena lo automático hacia el cliente (HU-ARCH-37, D-76).

Con el asistente en pausa no sale nada automático: respuestas del agente, seguimientos, recordatorios,
avisos de cobro y envíos a segmento. Lo que escribe una persona desde el panel o la app
(`sender_type="human"`) sigue saliendo; los envíos a segmento también usan "human", pero son
automáticos y se reconocen por su `client_message_id`. Lo frenado queda `failed` con el motivo y no se
reenvía al reactivar.

No mira `conversation.bot_paused`: el escalamiento la enciende justo antes de mandar su aviso, y los jobs
ya la miran por su cuenta. La pausa de la conexión es otra (ADR-0122, `meta_connection_state`) y frena
también a las personas.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.db.models import AppSettings, Conversation, Customer
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

MOTIVO_NEGOCIO_PAUSADO = "negocio_pausado"
MOTIVO_CLIENTE_PAUSADO = "cliente_pausado"
MOTIVOS = frozenset({MOTIVO_NEGOCIO_PAUSADO, MOTIVO_CLIENTE_PAUSADO})
# = `core.admin.envios_segmento.PREFIJO` (un test fija que coinciden; aquí sin importar el módulo).
_PREFIJO_SEGMENTO = "segmento:"


def es_envio_automatico(sender_type: str | None, client_message_id: str | None = None) -> bool:
    """Todo lo que no escribe una persona, más los envíos a segmento (que van como "human")."""
    if sender_type != "human":
        return True
    return bool(client_message_id) and str(client_message_id).startswith(_PREFIJO_SEGMENTO)


async def negocio_pausado(tenant_id: uuid.UUID) -> bool:
    """`app_settings.bot_paused` del negocio (una lectura; para el bucle por negocio de los crons)."""
    async with get_session() as session:
        pausado = (
            await session.execute(
                select(AppSettings.bot_paused).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    return bool(pausado)


async def motivo_pausa_automatica(conversation: Conversation, customer: Customer | None = None) -> str | None:
    """`negocio_pausado`, `cliente_pausado` o None. Primero el negocio, después el cliente."""
    tenant_id = conversation.tenant_id
    actual = get_current_tenant_id()
    if actual is not None and actual != tenant_id:
        # Defensa en profundidad (además de RLS): nunca se decide con datos de otro negocio.
        log.error("pausa_automatica_tenant_distinto", conversation_id=str(conversation.id))
        return None
    if await negocio_pausado(tenant_id):
        return MOTIVO_NEGOCIO_PAUSADO
    if customer is None or customer.id != conversation.customer_id:
        async with get_session() as session:
            customer = await get_owned(session, Customer, conversation.customer_id, tenant_id)
    if customer is not None and customer.bot_paused:
        return MOTIVO_CLIENTE_PAUSADO
    return None


async def motivo_pausa_por_id(conversation_id: uuid.UUID) -> str | None:
    """Como `motivo_pausa_automatica`, desde el id de la conversación (None si no existe en el negocio)."""
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        conversation = await get_owned(session, Conversation, conversation_id, tenant_id)
    if conversation is None:
        return None
    return await motivo_pausa_automatica(conversation)

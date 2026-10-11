"""¿Se puede usar una conexión de Meta? Pausa y borrado (HU-CONN-03, ADR-0122).

- **Viva:** sin `deleted_at`. Las acciones del panel (activar, desactivar, eliminar,
  facturación, diagnóstico, números, plantillas) solo operan sobre conexiones vivas.
- **Pausada:** desactivada a mano (`revoked`) o eliminada. Se guarda lo que entra, pero no
  corre la IA ni sale nada por ella, y nunca se cae a otra ruta (la primaria o el `.env`).
- `draft` (un alta sin terminar) tampoco envía.

Módulo sin dependencias del resto de `core.gateway` para que lo usen el sender, el
consumer y los servicios del panel sin importaciones circulares.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import ChannelConnection, MetaConnection
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

MOTIVO_PAUSADA = "La conexión de WhatsApp está desactivada"
MOTIVO_ELIMINADA = "La conexión de WhatsApp fue eliminada"
MOTIVO_SIN_ALTA = "La conexión de WhatsApp no terminó el alta"
# Razón fija de una conexión `pending` que solo espera el registro del número en la Cloud
# API con su PIN (HU-CONN-06, ADR-0127). Registrar el número la deja `connected`.
MOTIVO_NUMERO_SIN_REGISTRAR = "Falta registrar el número en la Cloud API con su PIN"


def esperando_registro(connection: Any) -> bool:
    """¿La conexión solo espera que se registre su número? (`pending` por el registro)."""
    return connection.status == "pending" and connection.status_reason == MOTIVO_NUMERO_SIN_REGISTRAR


def motivo_pausa(connection: Any) -> str | None:
    """Por qué la conexión no puede enviar ni correr la IA, o `None` si puede.

    `pending`, `connected`, `degraded` y `error` siguen enviando (ADR-0101)."""
    if connection.deleted_at is not None:
        return MOTIVO_ELIMINADA
    if connection.status == "revoked":
        return MOTIVO_PAUSADA
    if connection.status == "draft":
        return MOTIVO_SIN_ALTA
    return None


async def motivo_pausa_de(meta_connection_id: uuid.UUID | None) -> str | None:
    """`motivo_pausa` leyendo la conexión. Sin conexión (ruta de la plataforma) no hay pausa;
    una conexión que no existe (o es de otro negocio, RLS) cuenta como eliminada."""
    if meta_connection_id is None:
        return None
    async with get_session() as session:
        connection = await session.get(MetaConnection, meta_connection_id)
    # Filtro de negocio explícito además de RLS: una cuenta de otro negocio cuenta como eliminada.
    tenant_id = get_current_tenant_id()
    if connection is None or (tenant_id is not None and getattr(connection, "tenant_id", tenant_id) != tenant_id):
        return MOTIVO_ELIMINADA
    return motivo_pausa(connection)


async def get_live_meta_connection(
    session: AsyncSession,
    connection_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    *,
    for_update: bool = False,
) -> MetaConnection | None:
    """Como `get_owned`, pero solo conexiones vivas (sin `deleted_at`).

    `get_owned` no puede filtrar `deleted_at`: se usa con modelos que no lo tienen."""
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    query = select(MetaConnection).where(
        MetaConnection.id == connection_id,
        MetaConnection.tenant_id == tid,
        MetaConnection.deleted_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def cuenta_meta_de_conversacion(conversation: Any) -> uuid.UUID | None:
    """La cuenta de Meta (`meta_connections.id`) por la que sale la conversación, o `None` si
    no sale por una (canal web, WhatsApp Web, conexión manual, número de la plataforma).

    Con conexión canónica (ADR-0115 §2) la cuenta es la de su fila de `channel_connections`
    (`meta_connection_id`), no `conversations.meta_connection_id`: si un número cambia de
    cuenta, la fila se religa y la conversación la sigue. Sin `connection_id`, la de siempre
    (el puente `conversations.meta_connection_id`). Una conexión que no se puede leer (otro
    negocio, RLS) no aporta cuenta, como antes: las conexiones sin cuenta no se pausan."""
    if conversation.channel == "web":
        return None
    if getattr(conversation, "connection_id", None) is None:
        return conversation.meta_connection_id
    return await _cuenta_de_su_conexion(conversation)


async def cuenta_de_plantillas_de_conversacion(conversation: Any) -> uuid.UUID | None:
    """La cuenta en cuya WABA se buscan las plantillas de la conversación: la misma con la que
    salen sus credenciales (`sender.resolve_credentials`). Con conexión canónica, la de la
    conexión; si no (sin conexión, o una manual con el puente puesto), el puente
    `conversations.meta_connection_id`, como antes."""
    if getattr(conversation, "connection_id", None) is not None:
        cuenta = await _cuenta_de_su_conexion(conversation)
        if cuenta is not None:
            return cuenta
    return conversation.meta_connection_id


async def _cuenta_de_su_conexion(conversation: Any) -> uuid.UUID | None:
    tenant_id = getattr(conversation, "tenant_id", None) or get_current_tenant_id()
    async with get_session() as session:
        return (
            await session.execute(
                select(ChannelConnection.meta_connection_id).where(
                    ChannelConnection.id == conversation.connection_id,
                    ChannelConnection.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()


async def motivo_pausa_de_conversacion(conversation: Any) -> str | None:
    """`motivo_pausa_de` de la cuenta de Meta de la conversación (`cuenta_meta_de_conversacion`).
    El canal web y las conversaciones de una conexión sin cuenta (WhatsApp Web, manual) no
    aplican. Es la guarda única de entrada y salida (ADR-0122 §2, ADR-0115 §2)."""
    return await motivo_pausa_de(await cuenta_meta_de_conversacion(conversation))


_AVISO_PAUSA_TTL_S = 24 * 60 * 60
TITULO_CONEXION_PAUSADA = "WhatsApp desactivado"


async def avisar_mensaje_en_conexion_pausada(
    tenant_id: uuid.UUID, meta_connection_id: uuid.UUID, motivo: str
) -> None:
    """Push a los dueños, una vez al día por conexión: entran mensajes que nadie responde.

    Si Redis no responde, no se manda: sin la llave, sería un push por mensaje."""
    from core.memory.working import get_redis
    from core.shared.logger import get_logger

    log = get_logger(__name__)
    try:
        primero = await get_redis().set(
            f"conexion_pausada:aviso:{tenant_id}:{meta_connection_id}", "1", nx=True, ex=_AVISO_PAUSA_TTL_S
        )
    except Exception:
        log.warning("aviso_conexion_pausada_sin_redis", meta_connection_id=str(meta_connection_id))
        return
    if not primero:
        return
    try:
        from core.tasks.push_tasks import notify_owners_push

        notify_owners_push.delay(
            str(tenant_id),
            TITULO_CONEXION_PAUSADA,
            f"Llegaron mensajes de clientes y el asistente no los responde: {motivo.lower()}. "
            "Reactívala en Conexiones.",
            {"type": "core.connection_paused", "connectionId": str(meta_connection_id)},
        )
    except Exception:
        log.warning("aviso_conexion_pausada_fallo", meta_connection_id=str(meta_connection_id), exc_info=True)

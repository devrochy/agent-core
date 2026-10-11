"""Conversación por (conexión, cliente) detrás de `AOS_CONNECTION_ROUTING` (ADR-0115 §5 y §6).

Punto de enganche del consumer (`_resolve_conversation`): elige la conversación abierta y
actualiza su ruta según la bandera global.

- **Apagada (camino anterior, el de siempre):** una conversación abierta por cliente, la más
  reciente; cada entrada reescribe su ruta (`connection_id` y el puente `meta_connection_id`)
  para responder por donde escribió. Al cambiar `connection_id` limpia lo que era de la
  conexión anterior: la release (`release_id`, `release_lane`) y el dueño tras un handoff
  (`active_agent_instance_id`, `owner_since`). Sus FKs compuestas incluyen la conexión y la
  rechazarían, y ya no aplican a la conexión nueva.
- **Encendida:** una conversación abierta por (conexión, cliente) y nunca se reescribe
  `connection_id`. Hasta el *contract* no hay índice único: si hay más de una abierta (apagar
  y volver a encender la bandera puede dejarlas) toma la más reciente.
  - Una conversación abierta del cliente que quedó **sin conexión** (el backfill no pudo
    asignarla porque su cuenta tiene varios números, o se abrió entre el despliegue y el
    backfill) y sale por la cuenta de esta conexión se **adopta**: recibe esta conexión en
    vez de abrir otra (no se pierde el contexto ni cuenta dos veces en la cuota). Con el chat
    web (`webchat`), la abierta sin conexión del visitante (canal `web`).
  - Sin conexión identificable (número de la plataforma, o un número todavía sin fila
    canónica) la conversación es la del cliente sin conexión, y solo se actualiza el puente
    de Meta, como en el camino anterior.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import column, select, table, update
from sqlalchemy.orm.attributes import set_committed_value

from core.config import get_settings
from core.db.models import ChannelConnection, Conversation
from core.gateway.connection_types import WEBCHAT
from core.shared.logger import get_logger

log = get_logger(__name__)

ESTADOS_ABIERTOS = ("open", "escalated")

# Columnas del Agent OS (migración 0070) que todavía no están en el modelo: se escriben con
# una tabla ligera para no tocar `Conversation` mientras otras HU lo extienden.
_conversaciones = table(
    "conversations",
    column("id"),
    column("tenant_id"),
    column("connection_id"),
    column("release_id"),
    column("release_lane"),
    column("active_agent_instance_id"),
    column("owner_since"),
)


def ruteo_por_conexion() -> bool:
    """`AOS_CONNECTION_ROUTING` (global, ADR-0112): apagada por defecto."""
    return get_settings().aos_connection_routing


def _abiertas(tenant_id: uuid.UUID, customer_id: uuid.UUID) -> Any:
    return select(Conversation).where(
        Conversation.tenant_id == tenant_id,
        Conversation.customer_id == customer_id,
        Conversation.status.in_(ESTADOS_ABIERTOS),
        Conversation.deleted_at.is_(None),
    )


async def _la_mas_reciente(session: Any, query: Any) -> Conversation | None:
    return (await session.execute(query.order_by(Conversation.started_at.desc()).limit(1))).scalars().first()


async def buscar_conversacion_abierta(
    session: Any,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    connection: ChannelConnection | None,
    *,
    por_conexion: bool,
) -> Conversation | None:
    """La conversación abierta (`open`/`escalated`) más reciente del cliente; con
    `por_conexion`, la de esa conexión (o la sin conexión si no hay conexión), adoptando la
    huérfana de su misma cuenta de Meta si no tiene una propia."""
    query = _abiertas(tenant_id, customer_id)
    if not por_conexion:
        return await _la_mas_reciente(session, query)
    if connection is None:
        return await _la_mas_reciente(session, query.where(Conversation.connection_id.is_(None)))

    propia = await _la_mas_reciente(session, query.where(Conversation.connection_id == connection.id))
    if propia is not None:
        return propia
    if connection.meta_connection_id is not None:
        sin_conexion = query.where(
            Conversation.connection_id.is_(None),
            Conversation.meta_connection_id == connection.meta_connection_id,
        )
    elif connection.type_id == WEBCHAT:
        # Chat web (ADR-0115 §4): el cliente es del canal `web`, así que toda conversación suya sin
        # conexión es de este widget de antes del backfill (hoy hay uno por negocio).
        sin_conexion = query.where(Conversation.connection_id.is_(None), Conversation.channel == "web")
    else:
        return None
    huerfana = await _la_mas_reciente(session, sin_conexion)
    if huerfana is not None:
        # Sin conexión no puede tener release ni dueño (CHECK `ck_conv_aos_requiere_conexion`):
        # basta con asignarla.
        huerfana.connection_id = connection.id
        log.info(
            "conversacion_huerfana_adoptada", conversation_id=str(huerfana.id), connection_id=str(connection.id)
        )
    return huerfana


async def actualizar_ruta(
    session: Any,
    conversation: Conversation,
    connection_id: uuid.UUID | None,
    meta_connection_id: uuid.UUID | None,
    *,
    por_conexion: bool,
) -> None:
    """Ruta de una conversación que ya existía, con la entrada actual. Ver el docstring del módulo.

    El llamador solo la invoca si la entrada trae una ruta identificable (conexión o número de
    Meta): sin ruta se conserva la que había."""
    if por_conexion:
        # La conversación ya es de esta conexión: no se reescribe. Sin conexión, el puente de
        # Meta sigue a la entrada como antes.
        if connection_id is None and conversation.meta_connection_id != meta_connection_id:
            conversation.meta_connection_id = meta_connection_id
        return

    if conversation.connection_id != connection_id:
        # Una sola sentencia: la nueva conexión y la limpieza de lo de la anterior van juntas,
        # porque la FK `(tenant_id, connection_id, release_id)` no es diferida y rechazaría la
        # conexión nueva con la release vieja todavía puesta.
        await session.execute(
            update(_conversaciones)
            .where(_conversaciones.c.tenant_id == conversation.tenant_id, _conversaciones.c.id == conversation.id)
            .values(
                connection_id=connection_id,
                release_id=None,
                release_lane=None,
                active_agent_instance_id=None,
                owner_since=None,
            )
        )
        log.info(
            "conversacion_cambia_de_conexion",
            conversation_id=str(conversation.id),
            desde=str(conversation.connection_id) if conversation.connection_id else None,
            hacia=str(connection_id) if connection_id else None,
        )
        # Ya está escrito: el ORM lo refleja sin volver a enviarlo.
        set_committed_value(conversation, "connection_id", connection_id)
    if conversation.meta_connection_id != meta_connection_id:
        conversation.meta_connection_id = meta_connection_id

"""Línea de tiempo del cliente (HU-CRM-04, ADR-0147).

Se arma al vuelo, sin tabla de historia: cada fuente trae a lo sumo `limit` eventos del cliente
posteriores al cursor y aquí se mezclan, se ordenan por `(at, source, id)` descendente y se corta
la página. Fuentes del core: conversaciones iniciadas, escalaciones, datos aprendidos (también los
reemplazados, para ver quién cambió qué) y notas del equipo. Los packs habilitados aportan las
suyas con `LeadProvider.timeline` (ADR-0056); un pack que falla o tarda se omite y la respuesta
sale `partial`.

Las notas que el agente deja en una conversación (`Conversation.state['notes']`) no entran: no
tienen fecha y no se pueden paginar en SQL (ADR-0147 §4).
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select

from core.admin.lead_registry import Cursor, LeadProvider, TimelineEvent, antes_del_cursor
from core.db.models import (
    ChannelConnection,
    Conversation,
    CustomerNote,
    Escalation,
    MemoryFact,
    Message,
    SegmentSend,
    SegmentSendRecipient,
)
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

TIMEOUT_PACK_S = 3.0
_ORIGEN = {"tool": "el asistente", "agent": "el asistente", "human": "una persona", "import": "una importación"}
_ESTADO_MENSAJE = {
    "queued": "en cola",
    "sent": "enviada",
    "delivered": "entregada",
    "read": "leída",
    "failed": "falló",
}


def codificar_cursor(evento: TimelineEvent) -> str:
    crudo = json.dumps([evento.at.isoformat(), evento.source, evento.id])
    return base64.urlsafe_b64encode(crudo.encode()).decode().rstrip("=")


def decodificar_cursor(texto: str) -> Cursor:
    """`ValueError` si el cursor no es uno que hayamos emitido."""
    try:
        relleno = "=" * (-len(texto) % 4)
        at, source, id_ = json.loads(base64.urlsafe_b64decode(texto + relleno))
        momento = datetime.fromisoformat(at)
    except Exception as exc:  # cualquier forma inválida es el mismo error para el cliente
        raise ValueError("cursor inválido") from exc
    if momento.tzinfo is None or not isinstance(source, str) or not isinstance(id_, str):
        raise ValueError("cursor inválido")
    return momento, source, id_


def _corto(valor: Any, largo: int = 120) -> str:
    texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
    return texto if len(texto) <= largo else texto[: largo - 1] + "…"


async def _del_core(tenant_id: uuid.UUID, customer_id: uuid.UUID, cursor: Cursor | None, limit: int) -> list[TimelineEvent]:
    from core.api.routers.escalations import _habilidad_de_tool
    from core.orchestrator import motivos_escalacion as motivos

    eventos: list[TimelineEvent] = []
    async with get_session() as session:
        conversaciones = (
            await session.execute(
                select(Conversation.id, Conversation.started_at, Conversation.channel, ChannelConnection.label)
                .outerjoin(
                    ChannelConnection,
                    (ChannelConnection.id == Conversation.connection_id) & (ChannelConnection.tenant_id == tenant_id),
                )
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.customer_id == customer_id,
                    Conversation.deleted_at.is_(None),
                    antes_del_cursor(Conversation.started_at, Conversation.id, "core.conversacion", cursor),
                )
                .order_by(Conversation.started_at.desc(), Conversation.id.desc())
                .limit(limit)
            )
        ).all()
        for cid, at, canal, label in conversaciones:
            eventos.append(
                TimelineEvent(
                    at=at, source="core.conversacion", id=str(cid), kind="conversacion",
                    title=f"Escribió por {label or canal}", link_kind="conversation", link_id=str(cid),
                )
            )

        escalaciones = (
            await session.execute(
                select(Escalation)
                .join(Conversation, Conversation.id == Escalation.conversation_id)
                .where(
                    Escalation.tenant_id == tenant_id,
                    Conversation.tenant_id == tenant_id,
                    Conversation.customer_id == customer_id,
                    Escalation.deleted_at.is_(None),
                    antes_del_cursor(Escalation.created_at, Escalation.id, "core.escalacion", cursor),
                )
                .order_by(Escalation.created_at.desc(), Escalation.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for esc in escalaciones:
            eventos.append(
                TimelineEvent(
                    at=esc.created_at, source="core.escalacion", id=str(esc.id), kind="escalacion",
                    title=f"Escalación: {motivos.etiqueta(esc.reason, esc.summary, _habilidad_de_tool)}",
                    detail=esc.summary or None, link_kind="conversation", link_id=str(esc.conversation_id),
                )
            )

        hechos = (
            await session.execute(
                select(MemoryFact)
                .where(
                    MemoryFact.tenant_id == tenant_id,
                    MemoryFact.customer_id == customer_id,
                    MemoryFact.deleted_at.is_(None),
                    antes_del_cursor(MemoryFact.created_at, MemoryFact.id, "core.dato", cursor),
                )
                .order_by(MemoryFact.created_at.desc(), MemoryFact.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for h in hechos:
            eventos.append(
                TimelineEvent(
                    at=h.created_at, source="core.dato", id=str(h.id), kind="dato",
                    title=f"Dato guardado por {_ORIGEN.get(h.source, h.source)}: {h.key}",
                    detail=_corto(h.value),
                )
            )

        notas = (
            await session.execute(
                select(CustomerNote)
                .where(
                    CustomerNote.tenant_id == tenant_id,
                    CustomerNote.customer_id == customer_id,
                    CustomerNote.deleted_at.is_(None),
                    antes_del_cursor(CustomerNote.created_at, CustomerNote.id, "core.nota", cursor),
                )
                .order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for n in notas:
            eventos.append(
                TimelineEvent(
                    at=n.created_at, source="core.nota", id=str(n.id), kind="nota",
                    title=f"Nota de {n.author_label or 'el equipo'}", detail=_corto(n.text, 300),
                )
            )

        # HU-CRM-06 (ADR-0150, enmienda a ADR-0147): plantillas enviadas al cliente, de un envío a
        # un segmento, de una persona en la conversación o de un aviso por evento.
        plantillas = (
            await session.execute(
                select(
                    Message.id, Message.created_at, Message.template_name, Message.status,
                    Message.conversation_id, SegmentSend.admin_label,
                )
                .join(Conversation, and_(Conversation.id == Message.conversation_id, Conversation.tenant_id == tenant_id))
                .outerjoin(
                    SegmentSendRecipient,
                    and_(SegmentSendRecipient.message_id == Message.id, SegmentSendRecipient.tenant_id == tenant_id),
                )
                .outerjoin(
                    SegmentSend,
                    and_(SegmentSend.id == SegmentSendRecipient.send_id, SegmentSend.tenant_id == tenant_id),
                )
                .where(
                    Message.tenant_id == tenant_id,
                    Conversation.customer_id == customer_id,
                    Conversation.deleted_at.is_(None),
                    Message.direction == "outbound",
                    Message.template_name.is_not(None),
                    antes_del_cursor(Message.created_at, Message.id, "core.plantilla", cursor),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
        ).all()
        for mid, at, nombre, estado, conv_id, quien in plantillas:
            eventos.append(
                TimelineEvent(
                    at=at, source="core.plantilla", id=str(mid), kind="plantilla",
                    title=f"Plantilla enviada: {nombre}",
                    detail=" · ".join(
                        x for x in (f"Envío a un segmento de {quien or 'el equipo'}" if quien is not None else None,
                                    _ESTADO_MENSAJE.get(estado or "", estado)) if x
                    ) or None,
                    link_kind="conversation", link_id=str(conv_id),
                )
            )
    return eventos


async def _de_pack(
    provider: LeadProvider, customer_id: uuid.UUID, cursor: Cursor | None, limit: int
) -> list[TimelineEvent] | None:
    assert provider.timeline is not None
    try:
        eventos = await asyncio.wait_for(provider.timeline(customer_id, cursor, limit), TIMEOUT_PACK_S)
    except Exception:
        log.warning("lead_timeline_provider_failed", plugin_id=provider.plugin_id, exc_info=True)
        return None
    validos = [e for e in eventos if e.at.tzinfo is not None]
    if len(validos) != len(eventos):
        log.warning("lead_timeline_evento_sin_zona", plugin_id=provider.plugin_id)
    return validos


async def armar(
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    providers: list[LeadProvider],
    cursor: Cursor | None,
    limit: int,
) -> tuple[list[TimelineEvent], str | None, bool]:
    """(eventos de la página, cursor de la siguiente o None, partial).

    Cada fuente trae `limit + 1`: si en total sobra alguno, hay página siguiente."""
    por_fuente = limit + 1
    eventos = await _del_core(tenant_id, customer_id, cursor, por_fuente)
    partial = False
    for provider in providers:
        if provider.timeline is None:
            continue
        del_pack = await _de_pack(provider, customer_id, cursor, por_fuente)
        if del_pack is None:
            partial = True
            continue
        eventos.extend(del_pack)
    eventos.sort(key=lambda e: (e.at, e.source, e.id), reverse=True)
    pagina = eventos[:limit]
    siguiente = codificar_cursor(pagina[-1]) if len(eventos) > limit else None
    return pagina, siguiente, partial

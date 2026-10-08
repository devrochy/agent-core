"""Conversaciones (lectura): `GET /api/v1/admin/conversations`.

Lista paginada y detalle con mensajes. Mismos datos que las vistas SSR
`/admin/conversaciones` y `/admin/conversaciones/{id}`. Requiere la sección
`conversations`. Las conversaciones del playground (canal de prueba) se excluyen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from core.db.models import Conversation, Customer, Escalation, Message
from core.db.session import get_session
from core.shared.exceptions import OutboundWindowExpiredError
from core.shared.test_mode import TEST_CHANNEL

router = APIRouter(prefix="/admin/conversations", tags=["admin · conversaciones"])


class ConversationSummary(BaseModel):
    id: uuid.UUID
    customer_name: str | None
    external_id: str | None
    channel: str
    status: str
    bot_paused: bool
    started_at: datetime


class MessageItem(BaseModel):
    id: uuid.UUID
    direction: str
    sender_type: str
    content: str
    status: str
    created_at: datetime
    # HU-ARCH-13: plantilla con la que salió y motivo de un fallo (p. ej. `sin_plantilla`).
    template_name: str | None = None
    error_reason: str | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageItem]


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class BotState(BaseModel):
    bot_paused: bool


class ApprovedTemplate(BaseModel):
    """Plantilla aprobada de la conexión de la conversación (HU-ARCH-13)."""

    id: uuid.UUID
    name: str
    language: str
    category: str | None
    body: str
    variables: list[str] = Field(description="Placeholders del cuerpo, en orden.")


class TemplateMessageIn(BaseModel):
    template_id: uuid.UUID
    # placeholder ("1", "2" o el nombre) → texto que escribe la persona del equipo.
    variables: dict[str, str] = Field(default_factory=dict)


async def _owned_conversation(conversation_id: uuid.UUID, tenant_id) -> Conversation:
    """Conversación del tenant activo, o 404 (nunca de otro tenant)."""
    async with get_session() as session:
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
    if conversation is None:
        raise ApiError.not_found("Conversación no encontrada.")
    return conversation


def _summary(conv: Conversation, cust: Customer | None) -> ConversationSummary:
    return ConversationSummary(
        id=conv.id,
        customer_name=cust.name if cust else None,
        external_id=cust.external_id if cust else None,
        channel=conv.channel,
        status=conv.status,
        bot_paused=conv.bot_paused,
        started_at=conv.started_at,
    )


@router.get("", response_model=Page[ConversationSummary])
async def list_conversations(
    principal: Annotated[Principal, Depends(require("conversations"))],
    params: Annotated[PageParams, Depends(page_params)],
    status: Annotated[str | None, Query(description="open | escalated | closed")] = None,
    q: Annotated[str | None, Query(description="Busca por nombre o identificador.")] = None,
) -> Page[ConversationSummary]:
    tenant_id = principal.tenant_id
    query = (
        select(Conversation, Customer)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.channel != TEST_CHANNEL,
        )
    )
    if status:
        query = query.where(Conversation.status == status)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Customer.name.ilike(like), Customer.external_id.ilike(like)))

    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0
        rows = (
            await session.execute(
                query.order_by(Conversation.started_at.desc())
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).all()

    items = [_summary(conv, cust) for conv, cust in rows]
    return build_page(items, int(total), params)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations"))],
) -> ConversationDetail:
    tenant_id = principal.tenant_id
    async with get_session() as session:
        row = (
            await session.execute(
                select(Conversation, Customer)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.tenant_id == tenant_id,
                    Conversation.channel != TEST_CHANNEL,
                )
            )
        ).first()
        if row is None:
            raise ApiError.not_found("Conversación no encontrada.")
        conv, cust = row
        messages = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.tenant_id == tenant_id,
                    )
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )

    summary = _summary(conv, cust)
    return ConversationDetail(
        **summary.model_dump(),
        messages=[
            MessageItem(
                id=m.id,
                direction=m.direction,
                sender_type=m.sender_type,
                content=m.content,
                status=m.status,
                created_at=m.created_at,
                template_name=m.template_name,
                error_reason=m.error_reason,
            )
            for m in messages
        ],
    )


@router.post("/{conversation_id}/messages", status_code=204)
async def send_human_message(
    conversation_id: uuid.UUID,
    payload: MessageIn,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> Response:
    """Envía un mensaje como persona del equipo (handoff humano)."""
    conversation = await _owned_conversation(conversation_id, principal.tenant_id)
    if conversation.channel == TEST_CHANNEL:
        raise ApiError.bad_request("Las conversaciones de prueba no envían mensajes.", "test_channel")

    from core.gateway.sender import send_to_conversation

    try:
        await send_to_conversation(
            conversation_id, payload.content, sender_type="human", raise_on_window_expired=True
        )
    except OutboundWindowExpiredError as exc:
        # Antes `send_to_conversation` se tragaba el error y este 409 nunca salía
        # (HU-ARCH-13): el panel ofrece las plantillas de `GET .../templates`.
        raise ApiError(
            409,
            "window_expired",
            "La ventana de 24 h expiró: envía una plantilla aprobada.",
        ) from exc
    return Response(status_code=204)


@router.get("/{conversation_id}/templates", response_model=list[ApprovedTemplate])
async def list_conversation_templates(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations"))],
) -> list[ApprovedTemplate]:
    """Plantillas APROBADAS de la WABA de la conexión de esta conversación."""
    from core.gateway.event_notices import (
        approved_templates_for_conversation,
        render_body,
        template_has_non_body_variables,
        template_placeholders,
    )

    conversation = await _owned_conversation(conversation_id, principal.tenant_id)
    return [
        ApprovedTemplate(
            id=t.id,
            name=t.name,
            language=t.language,
            category=t.category,
            body=render_body(t, {}),
            variables=template_placeholders(t.components),
        )
        for t in await approved_templates_for_conversation(conversation)
        if not template_has_non_body_variables(t.components)
    ]


@router.post("/{conversation_id}/template-messages", status_code=204)
async def send_template_message(
    conversation_id: uuid.UUID,
    payload: TemplateMessageIn,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> Response:
    """Envía una plantilla aprobada como persona del equipo (fuera de la ventana)."""
    from core.gateway.event_notices import (
        approved_templates_for_conversation,
        build_components,
        render_body,
        template_placeholders,
    )
    from core.gateway.sender import send_template_to_conversation

    conversation = await _owned_conversation(conversation_id, principal.tenant_id)
    if conversation.channel == TEST_CHANNEL:
        raise ApiError.bad_request("Las conversaciones de prueba no envían mensajes.", "test_channel")
    plantilla = next(
        (
            t
            for t in await approved_templates_for_conversation(conversation)
            if t.id == payload.template_id
        ),
        None,
    )
    if plantilla is None:
        raise ApiError.not_found("Plantilla no aprobada en la conexión de esta conversación.")
    placeholders = template_placeholders(plantilla.components)
    valores = {k: v.strip() for k, v in payload.variables.items() if v and v.strip()}
    faltan = [p for p in placeholders if p not in valores]
    if faltan:
        raise ApiError.bad_request(
            "Completa todas las variables de la plantilla.",
            "template_variables",
            fields={p: "Requerida." for p in faltan},
        )
    valores = {p: valores[p][:1000] for p in placeholders}
    enviado = await send_template_to_conversation(
        conversation_id,
        template_name=plantilla.name,
        language=plantilla.language,
        components=build_components(plantilla, valores),
        preview_text=render_body(plantilla, valores),
        sender_type="human",
    )
    if not enviado:
        raise ApiError(409, "send_paused", "No se pudo enviar: la conexión está pausada por uso.")
    return Response(status_code=204)


@router.post("/{conversation_id}/toggle-bot", response_model=BotState)
async def toggle_conversation_bot(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> BotState:
    """Pausa o reanuda el agente en esta conversación (kill-switch por hilo)."""
    await _owned_conversation(conversation_id, principal.tenant_id)
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        conversation.bot_paused = not conversation.bot_paused
        paused = conversation.bot_paused
    return BotState(bot_paused=paused)


@router.post("/{conversation_id}/resolve", status_code=204)
async def resolve_conversation(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> Response:
    """Resuelve la conversación: reactiva el agente y cierra sus escalaciones."""
    await _owned_conversation(conversation_id, principal.tenant_id)
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        conversation.status = "open"
        conversation.bot_paused = False
        escalations = (
            (
                await session.execute(
                    select(Escalation).where(
                        Escalation.conversation_id == conversation_id,
                        Escalation.status != "resuelta",
                    )
                )
            )
            .scalars()
            .all()
        )
        for escalation in escalations:
            escalation.status = "resuelta"
            escalation.resolved_at = datetime.now(UTC)
    return Response(status_code=204)

"""Conversaciones: `GET /api/v1/admin/conversations`.

Lista paginada y detalle con mensajes. Requiere la sección `conversations`. Las
conversaciones del playground (canal de prueba) se excluyen.

Bandeja en vivo (HU-UX-15): la lista trae el último mensaje y la conexión y se ordena por
actividad; `GET …/{id}/messages?after=` da lo nuevo para el polling del panel; el detalle
trae la escalación activa, las notas internas y el costo; tomar, devolver al agente y las
notas de personas operan sobre la conversación.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, text, true

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from core.billing.tipos_de_uso import EVAL
from core.db.models import (
    Admin,
    ChannelConnection,
    Conversation,
    Customer,
    Escalation,
    Message,
    UsageLedger,
)
from core.db.session import get_owned, get_session
from core.kernel import toma_humana
from core.notifications import catalog
from core.notifications.service import notify_by_actor
from core.orchestrator import motivos_escalacion as motivos
from core.orchestrator.motivos_escalacion import MotivoCodigo
from core.shared.exceptions import OutboundWindowExpiredError
from core.shared.test_mode import TEST_CHANNEL

router = APIRouter(prefix="/admin/conversations", tags=["admin · conversaciones"])


# Largo del último mensaje en la lista (agent-sale corta en 64; aquí el panel trunca con CSS).
ULTIMO_MENSAJE_MAX = 160
# Tope de mensajes por consulta de `?after=` (el panel pide de nuevo si `has_more`).
MENSAJES_MAX = 200
# Solape del cursor: un mensaje con `created_at` anterior al cursor pero commiteado después de
# la consulta anterior se perdería con `>`; se re-entregan 2 s y el panel deduplica por `id`.
SOLAPE_CURSOR = timedelta(seconds=2)


class ConversationSummary(BaseModel):
    id: uuid.UUID
    customer_name: str | None
    external_id: str | None
    channel: str
    status: str
    bot_paused: bool
    started_at: datetime
    # HU-UX-15: último mensaje (recortado) y conexión por la que llegó el chat.
    last_message: str | None = None
    last_message_at: datetime | None = None
    last_message_sender: str | None = Field(default=None, description="customer | agent | human")
    connection_id: uuid.UUID | None = None
    connection_label: str | None = None
    connection_provider: str | None = None
    connection_type: str | None = Field(
        default=None, description="Tipo del catálogo (p. ej. whatsapp_cloud, whatsapp_waweb, web)"
    )


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
    # HU-UX-15: herramientas que usó el agente en el turno de este mensaje (en el primero
    # del turno), de `usage_ledger.tool_calls`.
    tool_calls: list[str] = Field(default_factory=list)
    # HU-AOS-28: el turno que produjo el mensaje del agente (abre el depurador, `GET /turns/{id}`).
    turn_id: uuid.UUID | None = Field(default=None, description="Turno del agente que lo envió; null salvo en los mensajes del agente.")


class MessagesSince(BaseModel):
    items: list[MessageItem]
    has_more: bool = Field(description="Hay más mensajes después del último devuelto")


class ActiveEscalation(BaseModel):
    """Escalación sin resolver más reciente de la conversación (el «ticket»)."""

    id: uuid.UUID
    reason_code: MotivoCodigo
    reason_label: str
    summary: str
    status: str = Field(description="pendiente | asignada")
    assigned_admin_id: uuid.UUID | None
    assigned_admin_email: str | None
    created_at: datetime


class ConversationNote(BaseModel):
    text: str
    source: Literal["agent", "human"]
    author_email: str | None = None
    created_at: datetime | None = None


class ConversationUsage(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    turns: int


class ConversationDetail(ConversationSummary):
    customer_id: uuid.UUID | None = None  # para abrir la ficha del cliente (HU-UX-16)
    messages: list[MessageItem]
    escalation: ActiveEscalation | None = None
    notes: list[ConversationNote] = Field(default_factory=list)
    usage: ConversationUsage = Field(
        default_factory=lambda: ConversationUsage(cost_usd=0, input_tokens=0, output_tokens=0, turns=0)
    )


class ConversationConnection(BaseModel):
    """Conexión por la que pueden llegar chats (opciones del filtro de la bandeja)."""

    id: uuid.UUID
    label: str
    provider: str
    type_id: str | None
    channel: str


class ConversationNoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


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


def _summary(
    conv: Conversation,
    cust: Customer | None,
    conexion: ChannelConnection | None = None,
    ultimo: tuple[str | None, datetime | None, str | None] = (None, None, None),
) -> ConversationSummary:
    contenido, fecha, remitente = ultimo
    return ConversationSummary(
        id=conv.id,
        customer_name=cust.name if cust else None,
        external_id=cust.external_id if cust else None,
        channel=conv.channel,
        status=conv.status,
        bot_paused=conv.bot_paused,
        started_at=conv.started_at,
        last_message=contenido[:ULTIMO_MENSAJE_MAX] if contenido is not None else None,
        last_message_at=fecha,
        last_message_sender=remitente,
        connection_id=conexion.id if conexion else None,
        connection_label=(conexion.label or None) if conexion else None,
        connection_provider=conexion.provider if conexion else None,
        connection_type=conexion.type_id if conexion else None,
    )


def _join_conexion():  # type: ignore[no-untyped-def]
    """LEFT JOIN de la conexión canónica (ADR-0115) de la conversación, del mismo tenant.

    Las conversaciones viejas que solo tienen `meta_connection_id` quedan sin conexión: con
    varios números en la misma cuenta de Meta no hay forma de saber cuál fue."""
    return and_(
        ChannelConnection.id == Conversation.connection_id,
        ChannelConnection.tenant_id == Conversation.tenant_id,
    )


async def _message_items(session: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID, messages: list[Message]) -> list[MessageItem]:
    """`MessageItem` con las herramientas del turno en el primer mensaje del agente de cada
    turno. Dos consultas en total, sin importar cuántos mensajes (sin N+1)."""
    turnos = {m.turn_id for m in messages if m.turn_id is not None and m.sender_type == "agent"}
    herramientas: dict[uuid.UUID, list[str]] = {}
    primero: dict[uuid.UUID, uuid.UUID] = {}
    if turnos:
        filas = (
            await session.execute(
                select(UsageLedger.turn_id, UsageLedger.tool_calls).where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.conversation_id == conversation_id,
                    UsageLedger.turn_id.in_(turnos),
                    UsageLedger.tool_calls.is_not(None),
                )
            )
        ).all()
        for turn_id, llamadas in filas:
            nombres = [str(c.get("tool")) for c in llamadas or [] if isinstance(c, dict) and c.get("tool")]
            herramientas.setdefault(turn_id, []).extend(nombres)
        if herramientas:
            # El primer mensaje del agente del turno, sobre TODA la conversación: así el polling
            # con `?after=` lo adjunta al mismo mensaje que la carga completa.
            primeros = (
                await session.execute(
                    select(Message.turn_id, Message.id)
                    .distinct(Message.turn_id)
                    .where(
                        Message.tenant_id == tenant_id,
                        Message.conversation_id == conversation_id,
                        Message.turn_id.in_(list(herramientas)),
                        Message.sender_type == "agent",
                        Message.deleted_at.is_(None),
                    )
                    .order_by(Message.turn_id, Message.created_at, Message.id)
                )
            ).all()
            primero = {turn_id: message_id for turn_id, message_id in primeros}
    return [
        MessageItem(
            id=m.id,
            direction=m.direction,
            sender_type=m.sender_type,
            content=m.content,
            status=m.status,
            created_at=m.created_at,
            template_name=m.template_name,
            error_reason=m.error_reason,
            tool_calls=herramientas.get(m.turn_id, []) if m.turn_id and primero.get(m.turn_id) == m.id else [],
            turn_id=m.turn_id if m.sender_type == "agent" else None,
        )
        for m in messages
    ]


def _notas(state: dict[str, Any] | None) -> list[ConversationNote]:
    """Notas de `state.notes`: las del agente son strings (`set_conversation_note`); las de
    personas, objetos con `source="human"`. Nada de esto se manda al cliente ni al modelo."""
    notas: list[ConversationNote] = []
    for nota in (state or {}).get("notes") or []:
        if isinstance(nota, str):
            if nota.strip():
                notas.append(ConversationNote(text=nota, source="agent"))
        elif isinstance(nota, dict) and str(nota.get("text") or "").strip():
            fecha = nota.get("at")
            try:
                creada = datetime.fromisoformat(fecha) if isinstance(fecha, str) else None
            except ValueError:
                creada = None
            notas.append(
                ConversationNote(
                    text=str(nota["text"]),
                    source="human" if nota.get("source") == "human" else "agent",
                    author_email=nota.get("author_email"),
                    created_at=creada,
                )
            )
    return notas


async def _escalacion_activa(session: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> ActiveEscalation | None:
    from core.api.routers.escalations import _habilidad_de_tool

    fila = (
        await session.execute(
            select(Escalation, Admin.email)
            .outerjoin(Admin, Admin.id == Escalation.assigned_admin_id)
            .where(
                Escalation.tenant_id == tenant_id,
                Escalation.conversation_id == conversation_id,
                Escalation.status != "resuelta",
                Escalation.deleted_at.is_(None),
            )
            .order_by(Escalation.created_at.desc())
            .limit(1)
        )
    ).first()
    if fila is None:
        return None
    esc, email = fila
    return ActiveEscalation(
        id=esc.id,
        reason_code=motivos.codigo(esc.reason, esc.summary),
        reason_label=motivos.etiqueta(esc.reason, esc.summary, _habilidad_de_tool),
        summary=esc.summary,
        status=esc.status,
        assigned_admin_id=esc.assigned_admin_id,
        assigned_admin_email=email,
        created_at=esc.created_at,
    )


@router.get("", response_model=Page[ConversationSummary])
async def list_conversations(
    principal: Annotated[Principal, Depends(require("conversations"))],
    params: Annotated[PageParams, Depends(page_params)],
    status: Annotated[str | None, Query(description="open | escalated | closed")] = None,
    q: Annotated[str | None, Query(description="Busca por nombre o identificador.")] = None,
    connection_id: Annotated[uuid.UUID | None, Query(description="Solo los chats de esta conexión")] = None,
) -> Page[ConversationSummary]:
    """Chats del negocio, del más reciente por actividad (último mensaje o inicio)."""
    tenant_id = principal.tenant_id
    base = (
        select(Conversation.id)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.channel != TEST_CHANNEL,
            Conversation.deleted_at.is_(None),
        )
    )
    if status:
        base = base.where(Conversation.status == status)
    if q:
        like = f"%{q}%"
        base = base.where(or_(Customer.name.ilike(like), Customer.external_id.ilike(like)))
    if connection_id is not None:
        base = base.where(Conversation.connection_id == connection_id)

    # Último mensaje por conversación: un LATERAL con LIMIT 1 que usa el índice
    # `ix_messages_tenant_conv_created (tenant_id, conversation_id, created_at)`.
    ultimo = (
        select(
            func.left(Message.content, ULTIMO_MENSAJE_MAX).label("content"),
            Message.created_at.label("created_at"),
            Message.sender_type.label("sender_type"),
        )
        .where(
            Message.tenant_id == Conversation.tenant_id,
            Message.conversation_id == Conversation.id,
            Message.deleted_at.is_(None),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
        .correlate(Conversation)
        .lateral("ultimo")
    )
    actividad = func.coalesce(ultimo.c.created_at, Conversation.started_at)
    query = (
        select(Conversation, Customer, ChannelConnection, ultimo.c.content, ultimo.c.created_at, ultimo.c.sender_type)
        .join(Customer, Customer.id == Conversation.customer_id)
        .outerjoin(ChannelConnection, _join_conexion())
        .outerjoin(ultimo, true())
        .where(Conversation.id.in_(base))
    )

    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar() or 0
        rows = (
            await session.execute(
                query.order_by(actividad.desc(), Conversation.id.desc())
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).all()

    items = [
        _summary(conv, cust, conexion, (contenido, fecha, remitente))
        for conv, cust, conexion, contenido, fecha, remitente in rows
    ]
    return build_page(items, int(total), params)


@router.get("/connections", response_model=list[ConversationConnection])
async def list_conversation_connections(
    principal: Annotated[Principal, Depends(require("conversations"))],
) -> list[ConversationConnection]:
    """Conexiones del negocio para filtrar la bandeja (HU-UX-15). Con el permiso de
    Conversaciones: quien atiende no necesita ver la sección Conexiones."""
    async with get_session() as session:
        filas = (
            (
                await session.execute(
                    select(ChannelConnection)
                    .where(
                        ChannelConnection.tenant_id == principal.tenant_id,
                        ChannelConnection.deleted_at.is_(None),
                        # La interna del Centro de pruebas no es un canal de clientes.
                        or_(ChannelConnection.type_id.is_(None), ChannelConnection.type_id != "playground"),
                    )
                    .order_by(ChannelConnection.created_at)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    return [
        ConversationConnection(
            id=c.id,
            label=c.label or c.external_id,
            provider=c.provider,
            type_id=c.type_id,
            channel=c.channel,
        )
        for c in filas
    ]


async def _conversacion_visible(session: Any, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[Conversation, Customer, ChannelConnection | None]:
    row = (
        await session.execute(
            select(Conversation, Customer, ChannelConnection)
            .join(Customer, Customer.id == Conversation.customer_id)
            .outerjoin(ChannelConnection, _join_conexion())
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.channel != TEST_CHANNEL,
                Conversation.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise ApiError.not_found("Conversación no encontrada.")
    return row[0], row[1], row[2]


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations"))],
) -> ConversationDetail:
    tenant_id = principal.tenant_id
    async with get_session() as session:
        conv, cust, conexion = await _conversacion_visible(session, conversation_id, tenant_id)
        messages = list(
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.tenant_id == tenant_id,
                        Message.deleted_at.is_(None),
                    )
                    .order_by(Message.created_at, Message.id)
                )
            )
            .scalars()
            .all()
        )
        items = await _message_items(session, tenant_id, conversation_id, messages)
        escalacion = await _escalacion_activa(session, tenant_id, conversation_id)
        costo, entrada, salida, turnos = (
            await session.execute(
                select(
                    func.coalesce(func.sum(UsageLedger.cost_usd), 0),
                    func.coalesce(func.sum(UsageLedger.input_tokens), 0),
                    func.coalesce(func.sum(UsageLedger.output_tokens), 0),
                    func.count(UsageLedger.id),
                ).where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.conversation_id == conversation_id,
                    UsageLedger.kind != EVAL,
                )
            )
        ).one()

    ultimo = messages[-1] if messages else None
    summary = _summary(
        conv,
        cust,
        conexion,
        (ultimo.content, ultimo.created_at, ultimo.sender_type) if ultimo else (None, None, None),
    )
    return ConversationDetail(
        **summary.model_dump(),
        customer_id=cust.id if cust is not None else None,
        messages=items,
        escalation=escalacion,
        notes=_notas(conv.state),
        usage=ConversationUsage(
            cost_usd=round(float(costo), 6),
            input_tokens=int(entrada),
            output_tokens=int(salida),
            turns=int(turnos),
        ),
    )


@router.get("/{conversation_id}/messages", response_model=MessagesSince)
async def list_messages_since(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations"))],
    after: Annotated[
        datetime | None,
        Query(
            description=(
                "`created_at` del último mensaje que ya tiene el panel, tal como lo devolvió la API. "
                "Se re-entregan los 2 s anteriores: el panel deduplica por `id`."
            )
        ),
    ] = None,
) -> MessagesSince:
    """Mensajes nuevos del hilo para el polling de la bandeja (HU-UX-15). Sin `after`, los
    últimos `MENSAJES_MAX`."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _conversacion_visible(session, conversation_id, tenant_id)
        query = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.tenant_id == tenant_id,
            Message.deleted_at.is_(None),
        )
        if after is not None:
            desde = after if after.tzinfo else after.replace(tzinfo=UTC)
            query = query.where(Message.created_at >= desde - SOLAPE_CURSOR)
            filas = list(
                (
                    await session.execute(
                        query.order_by(Message.created_at, Message.id).limit(MENSAJES_MAX + 1)
                    )
                )
                .scalars()
                .all()
            )
            has_more = len(filas) > MENSAJES_MAX
            filas = filas[:MENSAJES_MAX]
        else:
            filas = list(
                (
                    await session.execute(
                        query.order_by(Message.created_at.desc(), Message.id.desc()).limit(MENSAJES_MAX)
                    )
                )
                .scalars()
                .all()
            )
            filas.reverse()
            has_more = False
        items = await _message_items(session, tenant_id, conversation_id, filas)
    return MessagesSince(items=items, has_more=has_more)


async def _rechazar_si_conexion_pausada(conversation: Any) -> None:
    """409 si la conexión de WhatsApp de la conversación está desactivada o eliminada
    (ADR-0122): el operador lo ve en el acto, en vez de un mensaje fallido."""
    from core.gateway.meta_connection_state import motivo_pausa_de_conversacion

    # La cuenta sale de la conexión canónica si la conversación la tiene (ADR-0115 §2).
    motivo = await motivo_pausa_de_conversacion(conversation)
    if motivo is not None:
        raise ApiError(409, "connection_paused", f"{motivo}: reactívala en Conexiones para responder.")


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

    await _rechazar_si_conexion_pausada(conversation)
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
    await _rechazar_si_conexion_pausada(conversation)
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


async def _soltar(
    session: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID, escalations: list[Escalation], admin_id: uuid.UUID
) -> None:
    """`released` en las tareas abiertas si se cerró una escalación que alguien atendía (HU-AOS-28)."""
    atendida = next((e for e in escalations if e.assigned_admin_id is not None), None)
    if atendida is not None:
        await toma_humana.anotar(session, tenant_id, conversation_id, "released", admin_id=admin_id, escalation_id=atendida.id)


@router.post("/{conversation_id}/resolve", status_code=204)
async def resolve_conversation(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> Response:
    """Resuelve la conversación: reactiva el agente y cierra sus escalaciones."""
    await _owned_conversation(conversation_id, principal.tenant_id)
    async with get_session() as session:
        # Primero la conversación (orden de bloqueos conversación → tareas, ADR-0169 §5).
        conversation = await toma_humana.bloquear_conversacion(session, principal.tenant_id, conversation_id)
        assert conversation is not None
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
        await _soltar(session, principal.tenant_id, conversation_id, escalations, principal.admin.id)
        if escalations:
            await notify_by_actor(
                session,
                principal.tenant_id,
                catalog.handoff_resuelto(
                    escalations[0].id,
                    conversation_id,
                    principal.admin.id,
                    principal.admin.email,
                    devuelta_al_agente=False,
                ),
                principal.admin.id,
            )
    return Response(status_code=204)


@router.post("/{conversation_id}/take", response_model=ActiveEscalation)
async def take_conversation(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> ActiveEscalation:
    """Toma el chat escalado: la escalación activa queda `asignada` a quien la toma y el
    agente se pausa en esta conversación hasta devolverla (como `tomarTicket` de agent-sale).

    409 `no_escalation` si no hay escalación activa; 409 `already_taken` si la tiene otra
    persona del equipo (tomarla de nuevo uno mismo es idempotente)."""
    tenant_id = principal.tenant_id
    admin_id = principal.admin.id
    async with get_session() as session:
        await _conversacion_visible(session, conversation_id, tenant_id)
        # Primero la conversación (orden de bloqueos conversación → escalación → tareas).
        conversation = await toma_humana.bloquear_conversacion(session, tenant_id, conversation_id)
        esc = (
            await session.execute(
                select(Escalation)
                .where(
                    Escalation.tenant_id == tenant_id,
                    Escalation.conversation_id == conversation_id,
                    Escalation.status != "resuelta",
                    Escalation.deleted_at.is_(None),
                )
                .order_by(Escalation.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if esc is None:
            raise ApiError(409, "no_escalation", "Esta conversación no tiene una escalación pendiente.")
        if esc.assigned_admin_id is not None and esc.assigned_admin_id != admin_id:
            otro = await session.get(Admin, esc.assigned_admin_id)
            quien = otro.email if otro else "otra persona del equipo"
            raise ApiError(409, "already_taken", f"La conversación ya la atiende {quien}.")
        recien_tomada = esc.assigned_admin_id is None
        esc.assigned_admin_id = admin_id
        esc.status = "asignada"
        if conversation is None:
            conversation = await get_owned(session, Conversation, conversation_id, tenant_id)
        conversation.status = "escalated"
        conversation.bot_paused = True
        await session.flush()
        if recien_tomada:
            # HU-AOS-28: la toma queda en cada tarea abierta (solo la primera vez, `take` es idempotente).
            await toma_humana.anotar(session, tenant_id, conversation_id, "taken", admin_id=admin_id, escalation_id=esc.id)
            await notify_by_actor(
                session,
                tenant_id,
                catalog.handoff_tomado(esc.id, conversation_id, admin_id, principal.admin.email),
                admin_id,
            )
        tomada = await _escalacion_activa(session, tenant_id, conversation_id)
    assert tomada is not None
    return tomada


@router.post("/{conversation_id}/return-to-bot", status_code=204)
async def return_conversation_to_bot(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> Response:
    """Devuelve el chat al agente: cierra la atención humana (escalaciones `resuelta`), deja
    la conversación abierta y reactiva el agente (`reasignarTicketABot` de agent-sale)."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _conversacion_visible(session, conversation_id, tenant_id)
        conversation = await toma_humana.bloquear_conversacion(session, tenant_id, conversation_id)
        if conversation is None:
            conversation = await get_owned(session, Conversation, conversation_id, tenant_id)
        conversation.status = "open"
        conversation.bot_paused = False
        escalations = (
            (
                await session.execute(
                    select(Escalation).where(
                        Escalation.tenant_id == tenant_id,
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
        await _soltar(session, tenant_id, conversation_id, escalations, principal.admin.id)
        if escalations:
            await notify_by_actor(
                session,
                principal.tenant_id,
                catalog.handoff_resuelto(
                    escalations[0].id,
                    conversation_id,
                    principal.admin.id,
                    principal.admin.email,
                    devuelta_al_agente=True,
                ),
                principal.admin.id,
            )
    return Response(status_code=204)


@router.post("/{conversation_id}/notes", response_model=ConversationNote, status_code=201)
async def add_conversation_note(
    conversation_id: uuid.UUID,
    payload: ConversationNoteIn,
    principal: Annotated[Principal, Depends(require("conversations", write=True))],
) -> ConversationNote:
    """Nota interna de una persona del equipo (`source="human"`), junto a las del agente en
    `state.notes`. Nunca se envía al cliente ni entra al prompt."""
    texto = payload.text.strip()
    if not texto:
        raise ApiError.bad_request("Escribe la nota.", "empty_note")
    ahora = datetime.now(UTC)
    nota = {
        "text": texto,
        "source": "human",
        "author_id": str(principal.admin.id),
        "author_email": principal.admin.email,
        "at": ahora.isoformat(),
    }
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _conversacion_visible(session, conversation_id, tenant_id)
        # Append atómico: el bot puede escribir su nota a la vez (`set_conversation_note`).
        await session.execute(
            text(
                "UPDATE conversations SET state = jsonb_set(coalesce(state, '{}'::jsonb), '{notes}', "
                "coalesce(state->'notes', '[]'::jsonb) || cast(:nota as jsonb), true) "
                "WHERE id = :id AND tenant_id = :tenant_id"
            ),
            {"nota": json.dumps([nota]), "id": conversation_id, "tenant_id": tenant_id},
        )
    return ConversationNote(text=texto, source="human", author_email=principal.admin.email, created_at=ahora)

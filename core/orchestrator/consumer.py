"""Consumer del Redis Stream de mensajes entrantes.

Idempotencia Capa 2: consumer group con reintentos limitados y dead-letter.
Flujo por mensaje:
  1. Resolver/crear customer y conversation.
  2. Kill-switch (global > cliente > conversación): guardar mensaje, NO responder.
  3. Persistir inbound (idempotency_key = message_id del canal).
  4. Conversación escalada => solo guardar (la atiende un humano).
  5. Keywords de escalamiento => escalar + respuesta fija.
  6. Velocidad: inmediato => procesar ya; si no => schedule_debounce
     (el ACK se hace YA — el sorted set pasa a ser el dueño del turno).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.config import DEBOUNCE_DELAY_MS, get_settings
from core.db.models import (
    AppSettings,
    ChannelConnection,
    Conversation,
    Customer,
    Message,
    ProcessedEvent,
)
from core.db.session import get_session
from core.gateway.sender import send_to_conversation
from core.gateway.webhooks import INBOUND_STREAM
from core.memory.working import get_redis
from core.orchestrator.debounce import schedule_debounce
from core.orchestrator.escalation import (
    ESCALATION_REPLY,
    escalate,
    matches_escalation_keywords,
)
from core.orchestrator.loop import process_conversation
from core.orchestrator.turn_gate import run_turn
from core.shared.events import ON_CONVERSATION_STARTED, ON_MESSAGE_RECEIVED, event_bus
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

GROUP = "orchestrator-group"
CONSUMER = "orchestrator-1"
MAX_DELIVERIES = 3
DEAD_LETTER_STREAM = "agent_core:inbound:dlq"


async def ensure_group() -> None:
    r = get_redis()
    try:
        await r.xgroup_create(INBOUND_STREAM, GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def consume_loop() -> None:
    """Bucle infinito: task de fondo del proceso principal.

    Pipelining: entrega las entradas a la cola del grupo sin esperar a que
    termine el batch (xreadgroup '>' avanza aunque estén pendientes) y las
    procesa de forma concurrente acotada por el gate (global + por tenant).
    Así el mensaje de un tenant B se programa aunque A tenga cientos por delante:
    el cupo de A (max_turns_per_tenant) deja slots globales libres para B.
    """
    await ensure_group()
    r = get_redis()
    pending: set[asyncio.Task[None]] = set()
    max_inflight = get_settings().max_concurrent_turns * 5
    while True:
        try:
            batches = await r.xreadgroup(GROUP, CONSUMER, {INBOUND_STREAM: ">"}, count=10, block=2000)
            for _stream, entries in batches or []:
                for entry_id, fields in entries:
                    task = asyncio.create_task(_handle_entry(entry_id, fields))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
            if len(pending) >= max_inflight:
                _, pending = await asyncio.wait(
                    pending, timeout=2, return_when=asyncio.FIRST_COMPLETED
                )
                pending = set(pending)
        except asyncio.CancelledError:
            for task in pending:
                task.cancel()
            raise
        except Exception as exc:
            if "NOGROUP" in str(exc):
                # El stream/grupo se borró en runtime (flush/limpieza): recrear.
                await ensure_group()
            log.exception("consumer_loop_error")
            await asyncio.sleep(2)


async def _handle_entry(entry_id: str, fields: dict[str, str]) -> None:
    r = get_redis()
    retry_key = f"inbound:retries:{entry_id}"
    try:
        tenant_id = fields.get("tenant_id")
        await run_turn(tenant_id, handle_inbound(fields))
        await r.xack(INBOUND_STREAM, GROUP, entry_id)
        await r.delete(retry_key)
    except Exception:
        log.exception("inbound_handling_failed", entry_id=entry_id)
        retries = await r.incr(retry_key)
        await r.expire(retry_key, 3600)
        if retries >= MAX_DELIVERIES:
            await r.xadd(DEAD_LETTER_STREAM, fields)
            await r.xack(INBOUND_STREAM, GROUP, entry_id)
            await r.delete(retry_key)
            log.error("message_dead_lettered", entry_id=entry_id)


async def handle_inbound(fields: dict[str, str]) -> bool:
    """Procesa un inbound. Devuelve True si se procesó, False si era duplicado.

    ADR-0007: se inserta el claim en `processed_events` ANTES de procesar; si ya
    existe (único por tenant y message_id), el evento es un duplicado y se descarta
    (ACK arriba). ADR-0013: el claim es revocable para permitir el reintento del DLQ.
    """
    tenant_id = fields.get("tenant_id")
    if not tenant_id:
        log.warning("inbound_without_tenant", message_id=fields.get("external_message_id"))
        return False

    async with tenant_scope(tenant_id):
        if not await _claim_event(uuid.UUID(tenant_id), fields):
            log.info(
                "event_already_processed",
                message_id=fields.get("external_message_id"),
                tenant_id=tenant_id,
            )
            return False
        await _handle_inbound_scoped(fields)
        return True


async def _claim_event(tenant_id: uuid.UUID, fields: dict[str, str]) -> bool:
    """INSERT del claim de idempotencia. False = ya procesado (duplicado)."""
    async with get_session() as session:
        session.add(
            ProcessedEvent(
                tenant_id=tenant_id,
                provider=fields.get("provider", ""),
                external_message_id=fields.get("external_message_id", ""),
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            # El UNIQUE ya cortó: el evento es un duplicado. La transacción quedó
            # en rollback pendiente; se revierte aquí y el ACK del stream la cierra.
            await session.rollback()
            return False
    return True


async def _handle_inbound_scoped(fields: dict[str, str]) -> None:
    settings = get_settings()
    content = fields.get("content", "")
    media_context = fields.get("media_context", "")
    effective_message = f"{content}\n{media_context}".strip() if media_context else content

    conversation, customer, app_settings = await _resolve_conversation(fields)

    # ---------------- kill-switch en 3 niveles
    paused = (
        (app_settings.bot_paused if app_settings else False)
        or customer.bot_paused
        or conversation.bot_paused
    )

    # Persistir SIEMPRE el inbound (historial completo aunque el bot esté pausado)
    persisted = await _persist_inbound(conversation.id, fields, effective_message)
    if not persisted:
        return  # duplicado (Capa 3 de idempotencia: UNIQUE en Postgres)

    await event_bus.emit(
        ON_MESSAGE_RECEIVED,
        conversation_id=conversation.id,
        customer_id=customer.id,
        content=effective_message,
    )

    if paused or conversation.status == "escalated":
        return

    # ---------------- escalamiento por keywords
    keywords = list((app_settings.escalation_config if app_settings else {}).get("keywords", []))
    if matches_escalation_keywords(effective_message, keywords or None):
        await escalate(conversation.id, reason="keyword", summary=effective_message[:200])
        await send_to_conversation(conversation.id, ESCALATION_REPLY)
        return

    # ---------------- velocidad de respuesta
    behavior = dict(app_settings.behavior_config) if app_settings else {}
    mode = str(behavior.get("debounce_mode", settings.debounce_mode))
    delay_ms = DEBOUNCE_DELAY_MS.get(mode, 0)

    if delay_ms == 0:
        result = await process_conversation(conversation.id, effective_message)
        if result is not None and result.text:
            delivered = await send_to_conversation(
                conversation.id, result.text, image_url=result.image_url
            )
            if not delivered and not result.escalated:
                await escalate(conversation.id, reason="send_failed",
                               summary=result.text[:200])
    else:
        await schedule_debounce(conversation.id, delay_ms, {"latest_message": effective_message})


# ------------------------------------------------------------------ resolución
async def _resolve_conversation(
    fields: dict[str, str],
) -> tuple[Conversation, Customer, AppSettings | None]:
    channel = fields.get("channel", "whatsapp")
    external_id = fields.get("customer_external_id", "")
    name = fields.get("customer_name") or None
    connection_external_id = fields.get("connection_external_id", "")
    tenant_id = get_current_tenant_id()

    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        customer = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.channel == channel,
                    Customer.external_id == external_id,
                    Customer.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if customer is None:
            contact_phone = external_id.removeprefix("whatsapp:") if channel == "whatsapp" else None
            customer = Customer(
                tenant_id=tenant_id,
                channel=channel,
                external_id=external_id,
                name=name,
                contact_phone=contact_phone,
            )
            session.add(customer)
            await session.flush()
        elif name and not customer.name:
            customer.name = name

        connection = (
            await session.execute(
                select(ChannelConnection).where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.external_id == connection_external_id,
                )
            )
        ).scalar_one_or_none()

        conversation = (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.customer_id == customer.id,
                    Conversation.status.in_(["open", "escalated"]),
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        is_new = conversation is None
        if conversation is None:
            conversation = Conversation(
                tenant_id=tenant_id,
                customer_id=customer.id,
                channel=channel,
                connection_id=connection.id if connection else None,
            )
            session.add(conversation)
            await session.flush()
        elif connection and conversation.connection_id != connection.id:
            conversation.connection_id = connection.id  # responder por donde escribió

    if is_new:
        await event_bus.emit(
            ON_CONVERSATION_STARTED, conversation_id=conversation.id, customer_id=customer.id
        )
    return conversation, customer, app_settings


async def _persist_inbound(
    conversation_id: uuid.UUID, fields: dict[str, str], effective_message: str
) -> bool:
    media: list[dict[str, Any]] = json.loads(fields.get("media", "[]") or "[]")
    try:
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=get_current_tenant_id(),
                    conversation_id=conversation_id,
                    direction="inbound",
                    sender_type="customer",
                    content=effective_message,
                    media_type=media[0]["type"] if media else None,
                    media_url=media[0].get("url") if media else None,
                    media_transcription=fields.get("media_context") or None,
                    idempotency_key=fields.get("external_message_id") or None,
                )
            )
        return True
    except IntegrityError:
        log.info("inbound_duplicate_ignored", message_id=fields.get("external_message_id"))
        return False

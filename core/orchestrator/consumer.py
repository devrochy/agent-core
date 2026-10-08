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
import os
import socket
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from core.agents.responses import load_response
from core.agents.spec import load_active_spec
from core.billing.enforcement import QuotaExceeded, enforce_metric
from core.config import DEBOUNCE_DELAY_MS, get_settings
from core.db.models import (
    AppSettings,
    ChannelConnection,
    Conversation,
    Customer,
    Message,
    PhoneNumber,
    ProcessedEvent,
    utcnow,
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
    within_business_hours,
)
from core.orchestrator.loop import process_conversation
from core.orchestrator.turn_gate import TurnQueueTimeout, run_turn
from core.shared.events import ON_CONVERSATION_STARTED, ON_MESSAGE_RECEIVED, event_bus
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

GROUP = "orchestrator-group"


def consumer_name() -> str:
    """Nombre de consumer único por réplica (HU-ARCH-06).

    Antes era `orchestrator-1` fijo: con dos réplicas, ambas compartían nombre y
    el reclamo de pendientes (`XAUTOCLAIM`) no podía distinguir cuál seguía vivo.
    """
    return f"orchestrator-{socket.gethostname()}-{os.getpid()}"[:120]


CONSUMER = consumer_name()
MAX_DELIVERIES = 3
DEAD_LETTER_STREAM = "agent_core:inbound:dlq"
# Cada cuánto se intenta el reclamo de pendientes huérfanos (HU-ARCH-06).
RECLAIM_EVERY_S = 30.0
# Resultado del procesamiento de un inbound (HU-ARCH-06).
TURN_PROCESSED = "processed"
TURN_DUPLICATE = "duplicate"
TURN_BUSY = "busy"
TURN_SKIPPED = "skipped"


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

    HU-ARCH-06: además de los mensajes NUEVOS, cada `RECLAIM_EVERY_S` se reclaman
    con `XAUTOCLAIM` los pendientes huérfanos (crash, deploy, turno colgado), para
    que ningún mensaje quede sin procesar ni sin llegar a la DLQ.
    """
    await ensure_group()
    r = get_redis()
    pending: set[asyncio.Task[None]] = set()
    max_inflight = get_settings().max_concurrent_turns * 5
    last_reclaim = 0.0
    while True:
        try:
            batches = await r.xreadgroup(GROUP, CONSUMER, {INBOUND_STREAM: ">"}, count=10, block=2000)
            for _stream, entries in batches or []:
                for entry_id, fields in entries:
                    task = asyncio.create_task(_handle_entry(entry_id, fields))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
            now = asyncio.get_running_loop().time()
            if now - last_reclaim >= RECLAIM_EVERY_S:
                last_reclaim = now
                await _reclaim_pending(r, pending)
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


async def _reclaim_pending(r: Any, pending: set[asyncio.Task[None]]) -> None:
    """Reclama pendientes con idle > `inbound_reclaim_idle_ms` (HU-ARCH-06).

    El XAUTOCLAIM es una llamada corta; el procesamiento de cada entrada
    reclamada se agenda como task concurrente, igual que el camino de mensajes
    nuevos. Un pendiente que OTRO worker sigue procesando se salta en
    `_claim_event` (claim reciente = "busy") sin ACK.
    """
    settings = get_settings()
    try:
        result = await r.xautoclaim(
            INBOUND_STREAM,
            GROUP,
            CONSUMER,
            min_idle_time=settings.inbound_reclaim_idle_ms,
            start_id="0-0",
            count=10,
        )
    except Exception:
        log.exception("inbound_reclaim_failed")
        return
    # redis-py: [next_cursor, entries] (>=5.x) o [next_cursor, entries, deleted] (7.x).
    entries = result[1] if isinstance(result, (list, tuple)) and len(result) >= 2 else []
    for item in entries or []:
        try:
            entry_id, fields = item[0], item[1]
        except (IndexError, TypeError):
            continue
        log.warning("inbound_reclaimed", entry_id=entry_id)
        task = asyncio.create_task(_handle_entry(entry_id, dict(fields), reclaimed=True))
        pending.add(task)
        task.add_done_callback(pending.discard)


async def _handle_entry(entry_id: str, fields: dict[str, str], *, reclaimed: bool = False) -> None:
    r = get_redis()
    retry_key = f"inbound:retries:{entry_id}"
    try:
        tenant_id = fields.get("tenant_id")
        outcome = await run_turn(tenant_id, handle_inbound(fields))
        if outcome == TURN_BUSY:
            # Otro worker lo está procesando: NO se ackea; el reclamo lo reintenta
            # cuando el claim quede abandonado (HU-ARCH-06).
            log.info("inbound_busy_skip", entry_id=entry_id, reclaimed=reclaimed)
            return
        await r.xack(INBOUND_STREAM, GROUP, entry_id)
        await r.delete(retry_key)
    except TurnQueueTimeout:
        # El cupo del tenant está saturado (HU-ARCH-27): el turno no llegó a
        # correr. No se ackea ni cuenta como fallo; el reclamo lo reintenta.
        log.warning("inbound_queue_timeout_skip", entry_id=entry_id, reclaimed=reclaimed)
        return
    except Exception as exc:
        log.exception("inbound_handling_failed", entry_id=entry_id)
        retries = await r.incr(retry_key)
        await r.expire(retry_key, 3600)
        # El turno falló: se revoca el claim para que el reclamo lo reintente sin
        # esperar al umbral de abandono (HU-ARCH-06). El contador gobierna los intentos.
        await _revoke_claim(fields)
        if retries >= MAX_DELIVERIES:
            await _dead_letter(r, entry_id, fields, retries, error=f"{type(exc).__name__}: {exc}")
            await r.delete(retry_key)


async def _dead_letter(
    r: Any, entry_id: str, fields: dict[str, str], deliveries: int, *, error: str
) -> None:
    """Manda el mensaje a la DLQ con el error y lo ackea (HU-ARCH-06).

    El reintento manual del panel (ADR-0013) revoca el claim y lo re-encola.
    El claim incompleto se revoca aquí para no bloquear ese reintento.
    """
    payload = dict(fields)
    payload["error"] = (error or "")[:500]
    payload["deliveries"] = str(deliveries)
    payload["failed_at"] = utcnow().isoformat()
    await r.xadd(DEAD_LETTER_STREAM, payload)
    await r.xack(INBOUND_STREAM, GROUP, entry_id)
    await _revoke_claim(fields)
    log.error("message_dead_lettered", entry_id=entry_id, deliveries=deliveries)


async def _revoke_claim(fields: dict[str, str]) -> None:
    """Soft-delete del claim de idempotencia (ADR-0013), best-effort.

    Abre su propio `tenant_scope`: `processed_events` tiene RLS forzada y este
    helper se llama fuera del scope del turno (HU-ARCH-06).
    """
    tenant_id = fields.get("tenant_id")
    if not tenant_id:
        return
    try:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                await session.execute(
                    update(ProcessedEvent)
                    .where(
                        ProcessedEvent.tenant_id == uuid.UUID(tenant_id),
                        ProcessedEvent.external_message_id == fields.get("external_message_id", ""),
                        ProcessedEvent.deleted_at.is_(None),
                    )
                    .values(deleted_at=utcnow())
                )
    except Exception:
        log.warning("claim_revoke_failed", exc_info=True)


async def handle_inbound(fields: dict[str, str]) -> str:
    """Procesa un inbound y devuelve el resultado del claim (HU-ARCH-06).

    - `TURN_PROCESSED`: procesado (o manejado: cuota, pausa, escalado por keyword).
    - `TURN_DUPLICATE`: ya estaba terminado; se descarta (ACK).
    - `TURN_BUSY`: hay un claim incompleto reciente (otro worker en vuelo); NO se
      ackea y el reclamo lo reintenta.
    - `TURN_SKIPPED`: sin tenant o tenant no operativo; se descarta (ACK).

    ADR-0007: el claim se inserta ANTES de procesar; ADR-0013: es revocable;
    ADR-0090: lleva estado (`completed_at`) para distinguir en vuelo de terminado.
    """
    tenant_id = fields.get("tenant_id")
    if not tenant_id:
        log.warning("inbound_without_tenant", message_id=fields.get("external_message_id"))
        return TURN_SKIPPED

    async with tenant_scope(tenant_id):
        # Ciclo de vida + billing: mensajes de un tenant suspendido/eliminado o
        # con el plan vencido se descartan (ACK) sin procesar (ADR-0001/0045).
        from core.billing.enforcement import is_tenant_operational

        if not await is_tenant_operational(uuid.UUID(tenant_id)):
            log.warning(
                "inbound_tenant_not_operational",
                tenant_id=tenant_id,
                message_id=fields.get("external_message_id"),
            )
            return TURN_SKIPPED
        claim = await _claim_event(uuid.UUID(tenant_id), fields)
        if claim == "busy":
            return TURN_BUSY
        if claim == "duplicate":
            log.info(
                "event_already_processed",
                message_id=fields.get("external_message_id"),
                tenant_id=tenant_id,
            )
            return TURN_DUPLICATE
        await _handle_inbound_scoped(fields)
        await _mark_event_completed(uuid.UUID(tenant_id), fields.get("external_message_id", ""))
        return TURN_PROCESSED


async def _claim_event(tenant_id: uuid.UUID, fields: dict[str, str]) -> str:
    """Claim de idempotencia con estado (HU-ARCH-06).

    - `claimed`: evento nuevo, o su claim incompleto quedó abandonado (más viejo
      que `claim_stale_seconds`) y se revocó para reprocesar.
    - `duplicate`: ya se procesó por completo.
    - `busy`: hay un claim incompleto reciente (otro worker en vuelo).
    """
    settings = get_settings()
    external_id = fields.get("external_message_id", "")
    provider = fields.get("provider", "")
    async with get_session() as session:
        existing = (
            await session.execute(
                select(ProcessedEvent).where(
                    ProcessedEvent.tenant_id == tenant_id,
                    ProcessedEvent.external_message_id == external_id,
                    ProcessedEvent.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.completed_at is not None:
                return "duplicate"
            stale_before = utcnow() - timedelta(seconds=settings.claim_stale_seconds)
            if existing.handled_at is None or existing.handled_at < stale_before:
                # Claim abandonado (proceso muerto a mitad): se revoca.
                existing.deleted_at = utcnow()
                await session.flush()
            else:
                return "busy"
        session.add(
            ProcessedEvent(
                tenant_id=tenant_id,
                provider=provider,
                external_message_id=external_id,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            # Carrera con otra réplica: la otra ganó el claim.
            await session.rollback()
            return "duplicate"
    return "claimed"


async def _mark_event_completed(tenant_id: uuid.UUID, external_id: str) -> None:
    """Marca el claim como terminado (HU-ARCH-06). Best-effort."""
    if not external_id:
        return
    try:
        async with get_session() as session:
            await session.execute(
                update(ProcessedEvent)
                .where(
                    ProcessedEvent.tenant_id == tenant_id,
                    ProcessedEvent.external_message_id == external_id,
                    ProcessedEvent.deleted_at.is_(None),
                )
                .values(completed_at=utcnow())
            )
    except Exception:
        log.warning("claim_complete_failed", exc_info=True)


async def _handle_inbound_scoped(fields: dict[str, str]) -> None:
    settings = get_settings()
    content = fields.get("content", "")
    media_context = fields.get("media_context", "")
    effective_message = f"{content}\n{media_context}".strip() if media_context else content

    try:
        conversation, customer, app_settings = await _resolve_conversation(fields)
    except QuotaExceeded as exc:
        # Bloqueo duro de cuota (ADR-0045): se descarta el inbound sin procesar.
        log.warning(
            "inbound_quota_exceeded",
            tenant_id=get_current_tenant_id(),
            metric=exc.metric,
            used=exc.used,
            limit=exc.limit,
        )
        return

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
    escalation_cfg = dict(app_settings.escalation_config or {}) if app_settings else {}
    keywords = list(escalation_cfg.get("keywords", []))
    if matches_escalation_keywords(effective_message, keywords or None):
        # Resolver el texto ANTES de escalar (auditor-multitenant): si
        # load_active_spec falla (ej. BD caída), no queremos dejar la
        # conversación ya marcada `escalated`/`bot_paused` sin haber podido
        # avisarle nada al cliente -- y el claim de idempotencia de este
        # evento ya se insertó en _claim_event, así que un reintento
        # posterior lo vería como duplicado y no volvería a intentar nada.
        # `load_active_spec()` sin tenant explícito resuelve el de la
        # conversación en curso vía el `tenant_scope` que `handle_inbound`
        # ya abrió más arriba en la pila (mismo patrón que loop.py:171).
        spec = await load_active_spec()
        escalation_text = await load_response(spec, "escalation") or ESCALATION_REPLY
        await escalate(conversation.id, reason="keyword", summary=effective_message[:200])
        await send_to_conversation(conversation.id, escalation_text)
        return

    # ---------------- horario de atención (ADR-0051)
    # Fuera de horario: se responde el mensaje configurado y NO se procesa el turno
    # con el LLM (ahorra costo y evita prometer atención que no hay). Sin config,
    # `within_business_hours` devuelve True (fail-open).
    business_hours = escalation_cfg.get("business_hours")
    if business_hours and not within_business_hours(business_hours):
        out_msg = str(escalation_cfg.get("out_of_hours_message") or "").strip()
        if out_msg:
            text = out_msg
            for key, value in (
                ("{assistant_name}", app_settings.assistant_name if app_settings else ""),
                ("{business_description}", app_settings.business_description if app_settings else ""),
            ):
                text = text.replace(key, value or "")
            delivered = await send_to_conversation(conversation.id, text)
            if not delivered:
                # Mismo criterio que el camino inmediato: si el envío falla, se
                # escala para que una persona retome (el claim de idempotencia ya
                # se quemó, no habrá reintento).
                await escalate(conversation.id, reason="send_failed", summary=text[:200])
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
            # Cuota de clientes (ADR-0045): solo bloquea el ALTA de un cliente
            # nuevo; los clientes existentes se siguen atendiendo. Se pasa la
            # sesión abierta para no anidar otra (RLS es transaction-scoped).
            await enforce_metric(tenant_id, "clientes", session=session)
            contact_phone = external_id.removeprefix("whatsapp:") if channel == "whatsapp" else None
            try:
                async with session.begin_nested():
                    customer = Customer(
                        tenant_id=tenant_id,
                        channel=channel,
                        external_id=external_id,
                        name=name,
                        contact_phone=contact_phone,
                    )
                    session.add(customer)
                    await session.flush()
            except IntegrityError:
                # Ráfaga de mensajes del mismo cliente procesados concurrentemente
                # (ADR-0013): otro turno ya ganó la carrera y creó el Customer.
                # El SAVEPOINT se revirtió solo; se relee la fila ganadora.
                customer = (
                    await session.execute(
                        select(Customer).where(
                            Customer.tenant_id == tenant_id,
                            Customer.channel == channel,
                            Customer.external_id == external_id,
                            Customer.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
        elif name and not customer.name:
            customer.name = name

        connection = (
            await session.execute(
                select(ChannelConnection)
                .where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.external_id == connection_external_id,
                    ChannelConnection.deleted_at.is_(None),
                )
                .order_by(ChannelConnection.created_at)
                .limit(1)
            )
        ).scalars().first()

        # Puente para conexiones del wizard de Embedded Signup (ADR-0030):
        # no tienen fila en channel_connections, pero sí una en phone_numbers.
        # Corre EN PARALELO al lookup de arriba, nunca lo reemplaza.
        # tenant_id explícito además de la unicidad global de phone_number_id
        # (defensa en profundidad, mismo criterio que get_owned() -- hallazgo
        # auditor-multitenant, 2026-09-22: la unicidad global es hoy la única
        # garantía real, no depender solo de eso).
        meta_phone = (
            await session.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.phone_number_id == connection_external_id,
                    PhoneNumber.tenant_id == tenant_id,
                )
                .order_by(PhoneNumber.created_at)
                .limit(1)
            )
        ).scalars().first()
        meta_connection_id = meta_phone.connection_id if meta_phone else None

        # Ráfaga del mismo cliente procesada en paralelo (una tarea por entrada del
        # batch): sin candado, cada tarea que no ve conversación abierta crearía la suya
        # y los mensajes quedarían en turnos distintos. Se serializa el get-or-create por
        # cliente hasta el commit de esta sesión, como `meta_asset_claims` (ADR-0095).
        await session.execute(
            text("select pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"conversation:{tenant_id}:{customer.id}"},
        )
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
            # Cuota de conversaciones (ADR-0045): solo bloquea ABRIR una nueva;
            # las conversaciones ya abiertas siguen respondiendo. Sesión abierta
            # reutilizada (mismo criterio que el gate de clientes).
            await enforce_metric(tenant_id, "conversaciones", session=session)
            conversation = Conversation(
                tenant_id=tenant_id,
                customer_id=customer.id,
                channel=channel,
                connection_id=connection.id if connection else None,
                meta_connection_id=meta_connection_id,
            )
            session.add(conversation)
            await session.flush()
        elif connection is not None or meta_connection_id is not None:
            # Responder por donde escribió: la conversación queda con la ruta de ESTA
            # entrada y nada más. Si el cliente pasó de WhatsApp Web (QR) al número de Meta
            # (o al revés), la conexión anterior queda obsoleta y `resolve_credentials`
            # prioriza `connection_id`: la respuesta salía por WhatsApp Web aunque el
            # cliente escribiera a Meta (visto en test, 2026-10-08). Sin ruta identificable
            # (número de la plataforma) se conserva la que había.
            nueva_conexion = connection.id if connection else None
            if conversation.connection_id != nueva_conexion:
                conversation.connection_id = nueva_conexion
            if conversation.meta_connection_id != meta_connection_id:
                conversation.meta_connection_id = meta_connection_id

    if is_new:
        await event_bus.emit(
            ON_CONVERSATION_STARTED, conversation_id=conversation.id, customer_id=customer.id
        )
    return conversation, customer, app_settings


_SERVICE_WINDOW_HOURS = 24


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
            # Ventana de 24h de WhatsApp: cada inbound la refresca --
            # validada por `sender.py::validate_service_window` antes de
            # encolar un envío libre.
            if fields.get("provider") == "meta" and fields.get("channel") == "whatsapp":
                conversation = await session.get(Conversation, conversation_id)
                if conversation is not None:
                    conversation.service_window_expires_at = utcnow() + timedelta(hours=_SERVICE_WINDOW_HOURS)
        return True
    except IntegrityError:
        log.info("inbound_duplicate_ignored", message_id=fields.get("external_message_id"))
        return False

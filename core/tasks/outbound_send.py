"""Tarea Celery de envío de salida -- corre en el proceso `celery-worker`
(ya separado del API en `docker-compose.yml`), no en el proceso que
atiende webhooks/HTTP.

Idempotencia: `sender.py::send_to_conversation` inserta la fila de
`Message` con `status='queued'` y `client_message_id` ANTES de encolar --
esta tarea relee la fila por `message_id` y hace no-op si ya está en un
estado terminal (protección extra más allá del índice único parcial de
`client_message_id`, para el caso de que la MISMA tarea de Celery se
reintente y ya haya llegado a completar el envío).

Rate limiting alineado a Meta: la decisión de reintentar (y con qué delay)
vive en `decide_retry`, una función PURA separada de la mecánica de Celery
-- así se puede testear "estimated_time_to_regain_access=3 -> 180000ms"
sin simular un `self` de Celery real.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, or_, select

from core.db.models import Conversation, Customer, Message, utcnow
from core.db.session import get_session
from core.gateway.channels.registry import outbound_adapter_for
from core.gateway.meta_errors import REENGAGEMENT_WINDOW_CODE, GraphError
from core.gateway.sender import resolve_credentials
from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.celery_app import celery

log = get_logger(__name__)

MAX_ATTEMPTS = 3  # `attempts: 3` del ticket (cola de salida)
_DEFAULT_RETRY_S = 60.0


@dataclass
class RetryDecision:
    action: Literal["retry", "fail_permanent"]
    countdown_s: float | None = None


def decide_retry(exc: GraphError, attempts_made: int, max_attempts: int = MAX_ATTEMPTS) -> RetryDecision:
    """Nunca usa el backoff genérico para códigos de rate limit de Meta --
    usa `estimated_time_to_regain_access` (ya convertido a ms por
    `meta_errors._retry_after_from_usage`) tal cual. 131047 (re-engagement)
    y AUTH nunca reintentan: ni el tiempo ni los reintentos cambian el
    resultado."""
    if exc.code == REENGAGEMENT_WINDOW_CODE or exc.category in ("PERMANENT", "AUTH"):
        return RetryDecision("fail_permanent")

    if attempts_made >= max_attempts:
        return RetryDecision("fail_permanent")

    if exc.category == "RATE_LIMIT":
        countdown_s = (exc.retry_after_ms or 60_000) / 1000
        return RetryDecision("retry", countdown_s)

    # TRANSIENT: backoff exponencial acotado (60s, 120s, 240s, tope 900s)
    countdown_s = min(_DEFAULT_RETRY_S * (2**attempts_made), 900.0)
    return RetryDecision("retry", countdown_s)


async def _is_earliest_queued(
    conversation_id: uuid.UUID, tenant_id: uuid.UUID, created_at, msg_id: uuid.UUID
) -> bool:
    """¿Es este mensaje el outbound 'queued' más antiguo de la conversación?

    El envío es estrictamente secuencial por conversación: un mensaje solo se
    envía cuando no hay otro outbound pendiente con (created_at, id) anterior.
    Si no es el más antiguo, la tarea sale como no-op y el predecesor lo
    encadena al terminar (orden garantizado dentro del turno y entre turnos:
    burbujas, avisos de calendario y respuestas humanas).
    """
    async with get_session() as session:
        earlier = await session.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Message.tenant_id == tenant_id,
                Message.direction == "outbound",
                Message.status == "queued",
                or_(
                    Message.created_at < created_at,
                    and_(Message.created_at == created_at, Message.id < msg_id),
                ),
            )
            .limit(1)
        )
        return earlier.scalar_one_or_none() is None


async def _enqueue_next(
    conversation_id: uuid.UUID,
    tenant_id: uuid.UUID,
    after_created_at,
    after_id: uuid.UUID,
) -> None:
    """Encadena el siguiente outbound 'queued' de la conversación (por orden).

    Solo hay una tarea viva por conversación en vuelo: al terminar un mensaje
    se encola el siguiente más antiguo, así la cadena avanza en orden y nunca
    se corta (aunque una burbuja falle de forma permanente, la siguiente sale).
    """
    async with get_session() as session:
        nxt = await session.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Message.tenant_id == tenant_id,
                Message.direction == "outbound",
                Message.status == "queued",
                or_(
                    Message.created_at > after_created_at,
                    and_(Message.created_at == after_created_at, Message.id > after_id),
                ),
            )
            .order_by(Message.created_at, Message.id)
            .limit(1)
        )
        next_id = nxt.scalar_one_or_none()
    if next_id is not None:
        outbound_send_task.delay(str(next_id), str(tenant_id))


_CLAIM_TTL_S = 300


async def _claim_outbound(
    message_id: uuid.UUID, conversation_id: uuid.UUID, tenant_id: uuid.UUID
) -> bool:
    """Claim atómico (Redis SETNX) para que UNA sola tarea envíe el mensaje.

    Un mensaje puede encolarse dos veces (batch + cadena) o ser redeliverado
    por Celery tras un crash; el claim evita el doble envío. Si Redis falla,
    se envía igual (garantía at-least-once, no peor que hoy).
    """
    from core.memory.working import get_redis

    try:
        claimed = await get_redis().set(
            f"outbound:claim:{tenant_id}:{conversation_id}:{message_id}",
            "1",
            nx=True,
            ex=_CLAIM_TTL_S,
        )
        return bool(claimed)
    except Exception:
        return True


async def _release_claim(
    message_id: uuid.UUID, conversation_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    from core.memory.working import get_redis

    try:
        await get_redis().delete(f"outbound:claim:{tenant_id}:{conversation_id}:{message_id}")
    except Exception:
        pass


async def _send_one(message_id: uuid.UUID, tenant_id: uuid.UUID) -> GraphError | None:
    """Devuelve el `GraphError` si falló (para que el caller decida retry/DLQ),
    `None` si se envió bien o si no había nada que hacer (idempotencia)."""
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            message = await session.get(Message, message_id)
            if message is None or message.status != "queued":
                return None  # ya terminal (sent/delivered/read/failed) -- no-op
            # `tenant_id` llega como string plano en los args de la tarea
            # Celery, sin ligadura criptográfica al `message_id` -- defensa
            # en profundidad además de RLS (hallazgo auditor-multitenant
            # 2026-09-20): si por bug/race el tenant pasado no es el dueño
            # real de la fila, se loguea fuerte en vez de un no-op
            # silencioso de RLS.
            if message.tenant_id != tenant_id:
                log.error(
                    "outbound_send_tenant_mismatch",
                    message_id=str(message_id), task_tenant_id=str(tenant_id),
                    message_tenant_id=str(message.tenant_id),
                )
                return None

            conversation = await session.get(Conversation, message.conversation_id)
            customer = await session.get(Customer, conversation.customer_id) if conversation else None
        if conversation is None or customer is None:
            return None

        # ORDEN: solo se envía si es el outbound 'queued' más antiguo de la
        # conversación. Si no, esta tarea es un no-op y el predecesor encadena
        # este mensaje cuando termine (envío secuencial por conversación).
        if not await _is_earliest_queued(
            message.conversation_id, tenant_id, message.created_at, message.id
        ):
            return None

        # CLAIM (Redis): evita doble envío si el mismo mensaje se encoló dos
        # veces (batch + cadena) o Celery lo redeliveró tras un crash.
        if not await _claim_outbound(message.id, message.conversation_id, tenant_id):
            return None

        provider, credentials = await resolve_credentials(
            conversation.connection_id, conversation.meta_connection_id, expected_tenant_id=tenant_id
        )
        # Canal `web` (HU-CHAN-02): sin proveedor externo; el adaptador solo
        # marca el mensaje como enviado y el widget lo lee por polling.
        if conversation.channel == "web":
            provider, credentials = "web", {}
        adapter = outbound_adapter_for(provider)

        try:
            if message.media_type == "image" and message.media_url:
                wamid = await adapter.send_image(credentials, customer.external_id, message.media_url, message.content)
            else:
                wamid = await adapter.send_text(credentials, customer.external_id, message.content)
        except GraphError as exc:
            await _release_claim(message.id, message.conversation_id, tenant_id)
            return exc
        except Exception as exc:  # transporte no clasificado (ej. httpx.ConnectError sin envolver)
            await _release_claim(message.id, message.conversation_id, tenant_id)
            return GraphError(f"Error de red enviando mensaje: {exc}", category="TRANSIENT", is_transient=True)

        async with get_session() as session:
            db_message = await session.get(Message, message_id)
            if db_message is not None:
                db_message.status = "sent"
                db_message.external_message_id = wamid or None
                db_message.status_updated_at = utcnow()

        await _release_claim(message.id, message.conversation_id, tenant_id)
        # Encadena el siguiente pendiente de la conversación (la cola avanza
        # en orden incluso si este mensaje fue un "batch" inicial).
        await _enqueue_next(message.conversation_id, tenant_id, message.created_at, message.id)
        return None


async def _mark_failed(
    message_id: uuid.UUID, tenant_id: uuid.UUID, exc: GraphError, *, dlq: bool
) -> tuple[uuid.UUID, object, uuid.UUID] | None:
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            db_message = await session.get(Message, message_id)
            if db_message is None:
                return None
            if db_message.tenant_id != tenant_id:
                log.error(
                    "outbound_mark_failed_tenant_mismatch",
                    message_id=str(message_id), task_tenant_id=str(tenant_id),
                    message_tenant_id=str(db_message.tenant_id),
                )
                return None
            db_message.status = "failed"
            db_message.error_code = exc.code
            db_message.error_subcode = exc.error_subcode
            db_message.error_fbtrace_id = exc.fbtrace_id
            db_message.error_reason = exc.message
            db_message.status_updated_at = utcnow()
            conversation_id = db_message.conversation_id
            created_at = db_message.created_at
            failed_id = db_message.id

        if dlq:
            log.error(
                "outbound_dlq", message_id=str(message_id), tenant_id=str(tenant_id),
                error_code=exc.code, error_subcode=exc.error_subcode, fbtrace_id=exc.fbtrace_id,
            )

        from core.orchestrator.escalation import escalate

        await escalate(conversation_id, reason="send_failed", summary=exc.message[:500])

    return conversation_id, created_at, failed_id


@celery.task(bind=True, max_retries=MAX_ATTEMPTS, name="core.outbound_send")
def outbound_send_task(self, message_id: str, tenant_id: str) -> None:
    error = run_async(_send_one(uuid.UUID(message_id), uuid.UUID(tenant_id)))
    if error is None:
        return

    decision = decide_retry(error, attempts_made=self.request.retries)
    if decision.action == "retry":
        raise self.retry(exc=error, countdown=decision.countdown_s)

    dlq = self.request.retries >= MAX_ATTEMPTS and error.category not in ("PERMANENT", "AUTH")
    info = run_async(_mark_failed(uuid.UUID(message_id), uuid.UUID(tenant_id), error, dlq=dlq))
    # La cadena no se corta: aunque esta burbuja falló para siempre, el
    # siguiente mensaje pendiente de la conversación sigue en orden.
    if info is not None:
        conversation_id, created_at, failed_id = info
        run_async(_enqueue_next(conversation_id, uuid.UUID(tenant_id), created_at, failed_id))

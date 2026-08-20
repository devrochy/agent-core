"""Debounce Scheduler (ADR-022 de agent-sale, portado).

Si el cliente manda varios mensajes seguidos, el asistente espera a que
termine de escribir y responde UNA vez con todo el contexto.

Mecánica (Redis Sorted Set):
- ZADD debounce:pending score=(now+delay) member=conversation_id
  ZADD sobre un member existente ACTUALIZA el score => el timer se reinicia gratis.
- Poller cada 1.5s: ZRANGEBYSCORE -inf now; claim atómico con ZREM
  (solo el proceso que logra el ZREM dispara el turno — safe multi-worker).
- Payload en clave aparte: debounce:payload:{conversation_id}.

Crash recovery: al arrancar, barrido de conversaciones abiertas cuyo último
mensaje es inbound sin timer vivo => se reprograman con delay 0.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

PENDING_KEY = "debounce:pending"
PAYLOAD_KEY = "debounce:payload:{conversation_id}"
POLL_INTERVAL_S = 1.5


async def schedule_debounce(conversation_id: uuid.UUID, delay_ms: int, payload: dict[str, Any]) -> None:
    r = get_redis()
    fire_at = int(time.time() * 1000) + delay_ms
    await r.set(PAYLOAD_KEY.format(conversation_id=conversation_id), json.dumps(payload))
    await r.zadd(PENDING_KEY, {str(conversation_id): fire_at})


async def cancel_debounce(conversation_id: uuid.UUID) -> None:
    r = get_redis()
    await r.zrem(PENDING_KEY, str(conversation_id))
    await r.delete(PAYLOAD_KEY.format(conversation_id=conversation_id))


async def poll_debounce_loop() -> None:
    """Bucle infinito: corre como task de fondo del proceso principal."""
    while True:
        try:
            await poll_debounce_once()
        except Exception:
            log.exception("debounce_poll_failed")
        await asyncio.sleep(POLL_INTERVAL_S)


async def poll_debounce_once() -> None:
    r = get_redis()
    now = int(time.time() * 1000)
    due = await r.zrangebyscore(PENDING_KEY, "-inf", now)
    for conversation_id_str in due:
        # Claim atómico: solo quien logra el ZREM dispara
        removed = await r.zrem(PENDING_KEY, conversation_id_str)
        if removed != 1:
            continue
        payload_key = PAYLOAD_KEY.format(conversation_id=conversation_id_str)
        raw_payload = await r.get(payload_key)
        await r.delete(payload_key)
        payload = json.loads(raw_payload) if raw_payload else {}
        await _fire_conversation(uuid.UUID(conversation_id_str), payload)


async def _fire_conversation(conversation_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Procesa el turno diferido. Sin reintento automático: si falla, el próximo
    mensaje del cliente genera un turno fresco (límite conocido, documentado)."""
    from core.gateway.sender import send_to_conversation
    from core.orchestrator.loop import process_conversation

    try:
        latest_message = str(payload.get("latest_message", ""))
        result = await process_conversation(conversation_id, latest_message)
        if result is not None and result.text:
            await send_to_conversation(conversation_id, result.text, image_url=result.image_url)
    except Exception:
        log.exception("debounce_fire_failed", conversation_id=str(conversation_id))


async def recover_orphaned_conversations() -> None:
    """Al arrancar: reprograma conversaciones con inbound pendiente sin timer vivo."""
    from sqlalchemy import select, text

    from core.db.session import get_session

    r = get_redis()
    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT ON (m.conversation_id)
                               m.conversation_id, m.direction, m.content
                        FROM messages m
                        JOIN conversations c ON c.id = m.conversation_id
                        WHERE c.status = 'open' AND c.bot_paused = false
                        ORDER BY m.conversation_id, m.created_at DESC
                        """
                    )
                )
            ).all()
        recovered = 0
        for conversation_id, direction, content in rows:
            if direction != "inbound":
                continue
            has_timer = await r.zscore(PENDING_KEY, str(conversation_id))
            if has_timer is None:
                await schedule_debounce(
                    uuid.UUID(str(conversation_id)), 0, {"latest_message": content or ""}
                )
                recovered += 1
        if recovered:
            log.info("orphaned_conversations_recovered", count=recovered)
    except Exception:
        log.exception("orphan_recovery_failed")

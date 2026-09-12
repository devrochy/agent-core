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

Incidente real (tenant `yopoldo`, 2026-09-12): el cliente mandó "Domicilio,
calle 68 # 29a 47" y 4s después "Pago en línea" (dos burbujas de WhatsApp,
ambas dentro de la ventana de debounce). `schedule_debounce` REEMPLAZABA el
payload pendiente en vez de agruparlo -- el turno que finalmente se disparó
solo vio "Pago en línea"; la dirección se perdió para siempre (seguía en
Postgres vía `_persist_inbound`, pero `loop.py::_load_history()` nunca la
relee si Redis ya tiene turnos cacheados). El asistente terminó
preguntando la dirección 3 veces en la misma conversación y escalando. Esto
contradice la decisión de ADR-0006 ("agrupar mensajes consecutivos en un
solo turno") -- `schedule_debounce` ahora ACUMULA los mensajes en vez de
reemplazarlos. `agent-sale` no tiene esta clase de bug porque su payload no
lleva texto en absoluto (solo IDs) y relee todo el historial desde
Postgres en cada disparo -- portar ese diseño completo es un cambio mayor
(firma de `process_conversation`), fuera de esta tanda.

La acumulación usa RPUSH sobre una lista Redis aparte (`debounce:messages:
{conversation_id}`), no un GET+merge+SET sobre el payload: `consumer.py`
crea una task de asyncio por entrada del batch (`count=10`), así que dos
mensajes del MISMO conversation_id pueden procesarse concurrentemente --
un GET+SET no atómico reintroduciría el mismo bug bajo esa concurrencia
(hallazgo de `auditor-multitenant`, 2026-09-12). RPUSH es atómico por
comando, no hay ventana de carrera. `LTRIM`/`MAX_ACCUMULATED_MESSAGES`
acota el crecimiento (cliente/bot en loop reseteando el timer para
siempre) y el `EXPIRE`/TTL en ambas claves evita que una quede viva para
siempre si el proceso muere entre el `RPUSH` y el disparo del turno.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable
from typing import Any

from core.memory.working import get_redis
from core.orchestrator.turn_gate import run_turn
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

PENDING_KEY = "debounce:pending"
PAYLOAD_KEY = "debounce:payload:{conversation_id}"
MESSAGES_KEY = "debounce:messages:{conversation_id}"
POLL_INTERVAL_S = 1.5
# Tope duro de mensajes acumulados por ráfaga (higiene de memoria/Redis
# compartido entre tenants, no un límite de negocio).
MAX_ACCUMULATED_MESSAGES = 20
# TTL de red de seguridad para payload/mensajes pendientes: bastante mayor
# que el debounce_mode más largo (30s, "pausado") para no expirar una
# ventana legítima, pero acotado para que un crash entre el RPUSH y el
# disparo del turno no deje la clave viva para siempre. Se refresca en
# cada mensaje nuevo, igual que el timer del ZADD.
PENDING_KEY_TTL_S = 300


async def schedule_debounce(conversation_id: uuid.UUID, delay_ms: int, payload: dict[str, Any]) -> None:
    r = get_redis()
    fire_at = int(time.time() * 1000) + delay_ms
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)

    # El texto del mensaje se acumula aparte (lista Redis, RPUSH atómico) --
    # el resto del payload (ej. tenant_id) no necesita acumularse, un
    # "último valor gana" es correcto porque es el mismo valor en cada
    # llamada de la misma conversación.
    new_message = payload.pop("latest_message", "")
    payload_key = PAYLOAD_KEY.format(conversation_id=conversation_id)
    messages_key = MESSAGES_KEY.format(conversation_id=conversation_id)

    pipe = r.pipeline()
    if new_message:
        pipe.rpush(messages_key, new_message)
        pipe.ltrim(messages_key, -MAX_ACCUMULATED_MESSAGES, -1)
        pipe.expire(messages_key, PENDING_KEY_TTL_S)
    pipe.set(payload_key, json.dumps(payload), ex=PENDING_KEY_TTL_S)
    pipe.zadd(PENDING_KEY, {str(conversation_id): fire_at})
    await pipe.execute()


async def cancel_debounce(conversation_id: uuid.UUID) -> None:
    r = get_redis()
    await r.zrem(PENDING_KEY, str(conversation_id))
    await r.delete(
        PAYLOAD_KEY.format(conversation_id=conversation_id),
        MESSAGES_KEY.format(conversation_id=conversation_id),
    )


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
    fires: list[Awaitable[Any]] = []
    for conversation_id_str in due:
        # Claim atómico: solo quien logra el ZREM dispara
        removed = await r.zrem(PENDING_KEY, conversation_id_str)
        if removed != 1:
            continue
        payload_key = PAYLOAD_KEY.format(conversation_id=conversation_id_str)
        messages_key = MESSAGES_KEY.format(conversation_id=conversation_id_str)
        raw_payload = await r.get(payload_key)
        messages = await r.lrange(messages_key, 0, -1)
        await r.delete(payload_key, messages_key)
        payload = json.loads(raw_payload) if raw_payload else {}
        if messages:
            payload["latest_message"] = "\n".join(messages)
        conversation_id = uuid.UUID(conversation_id_str)
        tenant_id = payload.get("tenant_id")
        # Gate de turnos (global + por tenant) y disparo concurrente.
        fires.append(run_turn(tenant_id, _fire_conversation(conversation_id, payload)))
    if fires:
        await asyncio.gather(*fires)


async def _fire_conversation(conversation_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Procesa el turno diferido. Sin reintento automático: si falla, el próximo
    mensaje del cliente genera un turno fresco (límite conocido, documentado)."""
    from core.gateway.sender import send_to_conversation
    from core.orchestrator.escalation import escalate
    from core.orchestrator.loop import process_conversation

    try:
        latest_message = str(payload.get("latest_message", ""))
        tenant_id = payload.get("tenant_id")
        if tenant_id is None:
            log.warning("debounce_without_tenant", conversation_id=str(conversation_id))
            return

        async with tenant_scope(tenant_id):
            # Ciclo de vida: un tenant suspendido/eliminado no genera respuestas
            # (el mensaje quedó encolado ANTES de la suspensión).
            from core.tenants import is_tenant_active

            if not await is_tenant_active(uuid.UUID(tenant_id)):
                log.info("debounce_tenant_not_active", conversation_id=str(conversation_id))
                return
            result = await process_conversation(conversation_id, latest_message)
            if result is not None and result.text:
                delivered = await send_to_conversation(
                    conversation_id, result.text, image_url=result.image_url
                )
                if not delivered and not result.escalated:
                    # El turno se generó pero no llegó al canal: escalar a humano
                    # para no perder al cliente (incidente de envío silencioso detectado en producción).
                    await escalate(conversation_id, reason="send_failed",
                                   summary=result.text[:200])
    except Exception:
        log.exception("debounce_fire_failed", conversation_id=str(conversation_id))


async def recover_orphaned_conversations() -> None:
    """Al arrancar: reprograma conversaciones con inbound pendiente sin timer vivo.

    Recorre los tenants uno a uno dentro de tenant_scope: la query es RLS-aware
    (ADR-0012), así que cada tenant recupera solo sus conversaciones.
    """
    from sqlalchemy import text

    from core.db.session import get_session

    r = get_redis()
    try:
        async with get_session() as session:
            tenant_ids = (
                await session.execute(
                    text("select id from tenants where deleted_at is null and status = 'active'")
                )
            ).scalars().all()

        recovered = 0
        for tenant_id in tenant_ids:
            async with tenant_scope(tenant_id):
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
                # DENTRO del tenant_scope (bug encontrado por auditor-multitenant,
                # 2026-09-12): `schedule_debounce` lee `get_current_tenant_id()`
                # para sembrar `tenant_id` en el payload -- afuera de este bloque
                # el ContextVar ya volvió a None y la recuperación quedaba
                # silenciosamente inoperante para TODOS los tenants (el payload
                # sin tenant_id hace que `_fire_conversation` descarte el turno).
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

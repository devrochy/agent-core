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

Ids de los inbound de la ráfaga (seguimiento #438 de HU-AOS-10): viajan en otra lista,
`debounce:inbound:{tenant_id}:{conversation_id}`, con el mismo RPUSH/LTRIM/EXPIRE en el mismo
pipeline. Lleva el UUID del negocio primero (convención de `docs/MULTITENANT.md`) para que la
purga del negocio la barra aunque ya no existan sus conversaciones. Al disparar, el turno
diferido los anota en `core_decisions.inbound_message_ids`; el reencolado por
`TurnQueueTimeout` los vuelve a empujar desde el payload, como el texto. Sin orden garantizado
si llega un mensaje entre el claim y el reencolado (igual que el texto).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable
from typing import Any

from core.kernel.decisions import anotar_entrada, en_turno
from core.memory.working import get_redis
from core.orchestrator.turn_gate import TurnQueueTimeout, run_turn
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

PENDING_KEY = "debounce:pending"
PAYLOAD_KEY = "debounce:payload:{conversation_id}"
MESSAGES_KEY = "debounce:messages:{conversation_id}"
INBOUND_IDS_KEY = "debounce:inbound:{tenant_id}:{conversation_id}"
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
    inbound_ids = [str(i) for i in payload.pop("inbound_message_ids", None) or []]
    payload_key = PAYLOAD_KEY.format(conversation_id=conversation_id)
    messages_key = MESSAGES_KEY.format(conversation_id=conversation_id)

    pipe = r.pipeline()
    if new_message:
        pipe.rpush(messages_key, new_message)
        pipe.ltrim(messages_key, -MAX_ACCUMULATED_MESSAGES, -1)
        pipe.expire(messages_key, PENDING_KEY_TTL_S)
    if inbound_ids and payload.get("tenant_id"):
        ids_key = INBOUND_IDS_KEY.format(
            tenant_id=payload["tenant_id"], conversation_id=conversation_id
        )
        pipe.rpush(ids_key, *inbound_ids)
        pipe.ltrim(ids_key, -MAX_ACCUMULATED_MESSAGES, -1)
        pipe.expire(ids_key, PENDING_KEY_TTL_S)
    pipe.set(payload_key, json.dumps(payload), ex=PENDING_KEY_TTL_S)
    pipe.zadd(PENDING_KEY, {str(conversation_id): fire_at})
    await pipe.execute()


async def cancel_debounce(conversation_id: uuid.UUID) -> None:
    r = get_redis()
    await r.zrem(PENDING_KEY, str(conversation_id))
    payload_key = PAYLOAD_KEY.format(conversation_id=conversation_id)
    claves = [payload_key, MESSAGES_KEY.format(conversation_id=conversation_id)]
    tenant_id = _tenant_del_payload(await r.get(payload_key)) or get_current_tenant_id()
    if tenant_id is not None:  # sin negocio conocido, la lista de ids vence por TTL
        claves.append(INBOUND_IDS_KEY.format(tenant_id=tenant_id, conversation_id=conversation_id))
    await r.delete(*claves)


def _tenant_del_payload(raw_payload: Any) -> str | None:
    """`tenant_id` del payload pendiente (no cambia en la vida de la conversación)."""
    if not raw_payload:
        return None
    try:
        tenant_id = json.loads(raw_payload).get("tenant_id")
    except (ValueError, AttributeError):
        return None
    return str(tenant_id) if tenant_id else None


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
    scheduled: list[tuple[str, dict[str, Any]]] = []
    for conversation_id_str in due:
        # Claim atómico: solo quien logra el ZREM dispara
        removed = await r.zrem(PENDING_KEY, conversation_id_str)
        if removed != 1:
            continue
        payload_key = PAYLOAD_KEY.format(conversation_id=conversation_id_str)
        messages_key = MESSAGES_KEY.format(conversation_id=conversation_id_str)
        # El negocio se lee antes (nombra la lista de ids); payload, texto e ids se leen y
        # borran en una transacción: un mensaje que llegue en medio no pierde su texto ni su id.
        tenant_del_payload = _tenant_del_payload(await r.get(payload_key))
        ids_key = (
            INBOUND_IDS_KEY.format(tenant_id=tenant_del_payload, conversation_id=conversation_id_str)
            if tenant_del_payload
            else None
        )
        pipe = r.pipeline(transaction=True)
        pipe.get(payload_key)
        pipe.lrange(messages_key, 0, -1)
        if ids_key is not None:
            pipe.lrange(ids_key, 0, -1)
            pipe.delete(payload_key, messages_key, ids_key)
        else:
            pipe.delete(payload_key, messages_key)
        leido = await pipe.execute()
        raw_payload, messages = leido[0], leido[1]
        inbound_ids: list[str] = list(dict.fromkeys(leido[2])) if ids_key is not None else []
        tenant_leido = _tenant_del_payload(raw_payload)
        if ids_key is None and tenant_leido:
            # El payload apareció entre el GET y la transacción: sus ids van con él.
            ids_key = INBOUND_IDS_KEY.format(tenant_id=tenant_leido, conversation_id=conversation_id_str)
            pipe = r.pipeline(transaction=True)
            pipe.lrange(ids_key, 0, -1)
            pipe.delete(ids_key)
            inbound_ids = list(dict.fromkeys((await pipe.execute())[0]))
        payload = json.loads(raw_payload) if raw_payload else {}
        if messages:
            payload["latest_message"] = "\n".join(messages)
        if inbound_ids:
            payload["inbound_message_ids"] = inbound_ids
        conversation_id = uuid.UUID(conversation_id_str)
        tenant_id = payload.get("tenant_id")
        # Gate de turnos (global + por tenant) y disparo concurrente.
        fires.append(run_turn(tenant_id, _fire_conversation(conversation_id, payload)))
        scheduled.append((conversation_id_str, payload))
    if fires:
        results = await asyncio.gather(*fires, return_exceptions=True)
        for (conversation_id_str, payload), result in zip(scheduled, results, strict=False):
            if isinstance(result, TurnQueueTimeout):
                # Cupo del tenant saturado (HU-ARCH-27): se reprograma el turno
                # (delay 0) en vez de perderlo. Nada se procesó todavía.
                log.warning("debounce_requeued_queue_timeout", conversation_id=conversation_id_str)
                await schedule_debounce(uuid.UUID(conversation_id_str), 0, dict(payload))


async def _fire_conversation(conversation_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Procesa el turno diferido. Sin reintento automático: si falla, el próximo
    mensaje del cliente genera un turno fresco (límite conocido, documentado)."""
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
            await _turno_diferido(
                conversation_id, latest_message, payload.get("inbound_message_ids") or []
            )
    except Exception:
        log.exception("debounce_fire_failed", conversation_id=str(conversation_id))


@en_turno  # decisión y resultado del turno (HU-AOS-10); el loop reusa este turno
async def _turno_diferido(
    conversation_id: uuid.UUID, latest_message: str, inbound_message_ids: list[str]
) -> None:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.kernel.egress import entregar
    from core.kernel.ingress import conexion_pausada
    from core.orchestrator.loop import process_conversation

    # Los inbound de la ráfaga, antes de la primera decisión (`core_decisions`).
    for message_id in inbound_message_ids:
        try:
            anotar_entrada(uuid.UUID(str(message_id)))
        except ValueError:
            continue
    # La conexión se pausó o eliminó después de agendar el turno (ADR-0122): no se
    # gasta el LLM ni se escala por un envío que no puede salir.
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
    if conversation is not None and await conexion_pausada(
        conversation, avisar_duenos=False, evento="debounce_conexion_pausada"
    ):
        return
    result = await process_conversation(conversation_id, latest_message)
    await entregar(conversation_id, result)


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
                                       m.conversation_id, m.id, m.direction, m.content
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
                for conversation_id, message_id, direction, content in rows:
                    if direction != "inbound":
                        continue
                    has_timer = await r.zscore(PENDING_KEY, str(conversation_id))
                    if has_timer is None:
                        await schedule_debounce(
                            uuid.UUID(str(conversation_id)),
                            0,
                            {"latest_message": content or "", "inbound_message_ids": [message_id]},
                        )
                        recovered += 1
        if recovered:
            log.info("orphaned_conversations_recovered", count=recovered)
    except Exception:
        log.exception("orphan_recovery_failed")

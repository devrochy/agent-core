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

Turno que falla (H2, mensajes perdidos con Twilio, 2026-11-20): antes `_fire_conversation`
tragaba toda excepción y un `TurnTimeout` se descartaba; como el payload ya se había leído y
borrado en el claim, el mensaje se perdía sin rastro. Ahora el payload se RESTAURA (se reencola
con `schedule_debounce`, igual que el camino de `TurnQueueTimeout`) en vez de borrarse al final:
borrar tarde perdería los mensajes que lleguen durante el turno (la clase de bug de `yopoldo`).
Cada fallo, `TurnTimeout` incluido, suma un intento (`debounce:intentos:{tenant}:{conv}`); al
llegar a `MAX_DELIVERIES` (el mismo tope del consumer) el turno va a la DLQ del stream de entrada
con la forma de un entrante, para que `POST /admin/dlq/{id}/retry` lo reprocese, y se avisa al
equipo. La recuperación de huérfanos corre además cada `ORPHAN_SWEEP_EVERY_S`, con guardas para
no duplicar un turno en curso ni re-disparar para siempre el mismo mensaje.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.config import get_settings
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
# Reintentos del turno diferido (H2). Las claves llevan el negocio primero para que la purga
# las barra (`core.tenants._es_del_tenant`).
ATTEMPTS_KEY = "debounce:intentos:{tenant_id}:{conversation_id}"
ATTEMPTS_TTL_S = 3600
# Espera antes de reintentar un turno fallido: crece con el intento (5 s, 10 s).
RETRY_DELAY_MS = 5_000
# Turno diferido en curso: el barrido periódico de huérfanos no lo vuelve a disparar.
IN_FLIGHT_KEY = "debounce:en_curso:{tenant_id}:{conversation_id}"
# Mensaje ya recuperado por el barrido periódico: no se recupera dos veces (un mensaje que una
# política deja sin respuesta a propósito no se re-dispara cada ciclo).
RECOVERED_KEY = "debounce:recuperado:{tenant_id}:{message_id}"
# Barrido periódico de huérfanos (además del de arranque) y su ventana: más viejo que lo que
# puede durar un turno (para no pisar uno en curso) y más nuevo que una hora.
ORPHAN_SWEEP_EVERY_S = 300
ORPHAN_MAX_AGE_S = 3600


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
    """Bucle infinito: corre como task de fondo del proceso principal.

    Cada `ORPHAN_SWEEP_EVERY_S` barre también los huérfanos (el barrido de arranque lo hace
    `core.main`; este cubre el turno que se perdió con la API ya corriendo).
    """
    last_sweep = time.monotonic()
    while True:
        try:
            await poll_debounce_once()
        except Exception:
            log.exception("debounce_poll_failed")
        if time.monotonic() - last_sweep >= ORPHAN_SWEEP_EVERY_S:
            last_sweep = time.monotonic()
            await recover_orphaned_conversations(periodico=True)
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
        if tenant_id:
            await r.set(
                IN_FLIGHT_KEY.format(tenant_id=tenant_id, conversation_id=conversation_id_str),
                "1",
                ex=_in_flight_ttl_s(),
            )
        # Gate de turnos (global + por tenant) y disparo concurrente.
        fires.append(run_turn(tenant_id, _fire_conversation(conversation_id, payload)))
        scheduled.append((conversation_id_str, payload))
    if not fires:
        return
    tasks = [asyncio.ensure_future(fire) for fire in fires]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        # Apagado del proceso con turnos en vuelo: los que no terminaron bien se restauran sin
        # contar intento (los que sí terminaron no se repiten).
        for (conversation_id_str, payload), task in zip(scheduled, tasks, strict=True):
            if task.done() and not task.cancelled() and task.exception() is None:
                continue
            await _restaurar(conversation_id_str, payload, motivo="cancelado")
        raise
    for (conversation_id_str, payload), result in zip(scheduled, results, strict=False):
        try:
            await _resolver_disparo(r, conversation_id_str, payload, result)
        except Exception:
            log.exception("debounce_resultado_sin_resolver", conversation_id=conversation_id_str)


def _in_flight_ttl_s() -> int:
    """Lo más que puede durar un turno diferido: espera del cupo + presupuesto del turno + margen."""
    settings = get_settings()
    return settings.turn_queue_timeout_s + settings.turn_timeout_s + 60


async def _restaurar(conversation_id_str: str, payload: dict[str, Any], *, motivo: str, delay_ms: int = 0) -> None:
    """Devuelve el payload al debounce (texto e ids incluidos): el turno no se pierde."""
    log.warning("debounce_turno_reencolado", conversation_id=conversation_id_str, motivo=motivo)
    await schedule_debounce(uuid.UUID(conversation_id_str), delay_ms, dict(payload))


async def _resolver_disparo(r: Any, conversation_id_str: str, payload: dict[str, Any], result: Any) -> None:
    """Qué pasa con el turno según cómo terminó (H2).

    - Bien: se borra el contador de intentos.
    - `TurnQueueTimeout` (cupo del tenant saturado, HU-ARCH-27) o cancelado: no llegó a correr
      (o lo cortó el apagado); se reencola sin contar intento.
    - Cualquier otra excepción, `TurnTimeout` incluido: cuenta un intento; se reencola hasta
      `MAX_DELIVERIES` y después va a la DLQ.
    """
    tenant_id = payload.get("tenant_id")
    attempts_key = (
        ATTEMPTS_KEY.format(tenant_id=tenant_id, conversation_id=conversation_id_str) if tenant_id else None
    )
    try:
        if not isinstance(result, BaseException):
            if attempts_key is not None:
                await r.delete(attempts_key)
            return
        if isinstance(result, TurnQueueTimeout):
            await _restaurar(conversation_id_str, payload, motivo="queue_timeout")
            return
        if isinstance(result, asyncio.CancelledError):
            await _restaurar(conversation_id_str, payload, motivo="cancelado")
            return
        if attempts_key is None:
            # Sin negocio no hay turno posible (`_fire_conversation` lo descarta antes de fallar).
            log.error("debounce_fallo_sin_tenant", conversation_id=conversation_id_str)
            return
        from core.orchestrator.consumer import MAX_DELIVERIES

        error = f"{type(result).__name__}: {result}"
        intentos = int(await r.incr(attempts_key))
        await r.expire(attempts_key, ATTEMPTS_TTL_S)
        log.error(
            "debounce_fire_failed",
            conversation_id=conversation_id_str,
            tenant_id=tenant_id,
            intento=intentos,
            error=type(result).__name__,  # el texto completo va solo a la DLQ (filtrada por negocio)
            exc_info=result,
        )
        if intentos < MAX_DELIVERIES:
            await _restaurar(
                conversation_id_str, payload, motivo="fallo", delay_ms=RETRY_DELAY_MS * intentos
            )
            return
        await _dead_letter_debounce(r, conversation_id_str, payload, intentos, error=error)
        await r.delete(attempts_key)
    finally:
        if tenant_id:
            await r.delete(IN_FLIGHT_KEY.format(tenant_id=tenant_id, conversation_id=conversation_id_str))


async def _dead_letter_debounce(
    r: Any, conversation_id_str: str, payload: dict[str, Any], deliveries: int, *, error: str
) -> None:
    """Manda el turno agotado a la DLQ del stream de entrada y avisa al equipo.

    Misma forma que `consumer._dead_letter` (campos del entrante + `error`, `deliveries`,
    `failed_at`), sin su `XACK`/`XDEL`: el turno diferido no tiene entrada en el stream. El
    `external_message_id` es sintético a propósito: el original ya está en `messages` y su índice
    único haría que el reintento del panel se descartara como duplicado sin correr el turno.
    """
    from core.db.models import utcnow
    from core.orchestrator.consumer import DEAD_LETTER_STREAM, _avisar_mensaje_fallido

    tenant_id = str(payload["tenant_id"])
    try:
        campos = await _campos_para_dlq(uuid.UUID(conversation_id_str), payload)
    except Exception:
        # Sin la conversación el reintento quizá no la resuelva, pero el mensaje queda a la vista.
        log.warning("debounce_dlq_sin_conversacion", conversation_id=conversation_id_str, exc_info=True)
        campos = _campos_minimos(conversation_id_str, payload)
    campos["error"] = (error or "")[:500]
    campos["deliveries"] = str(deliveries)
    campos["failed_at"] = utcnow().isoformat()
    entry_id = await r.xadd(DEAD_LETTER_STREAM, campos)
    log.error(
        "debounce_dead_lettered",
        conversation_id=conversation_id_str,
        tenant_id=tenant_id,
        entry_id=str(entry_id),
        deliveries=deliveries,
    )
    await _avisar_mensaje_fallido(tenant_id, str(entry_id))


def _campos_minimos(conversation_id_str: str, payload: dict[str, Any]) -> dict[str, str]:
    return {
        "tenant_id": str(payload.get("tenant_id") or ""),
        "provider": "",
        "channel": "",
        "external_message_id": f"debounce:{conversation_id_str}:{uuid.uuid4().hex}",
        "customer_external_id": "",
        "customer_name": "",
        "content": str(payload.get("latest_message") or ""),
        "connection_external_id": "",
        "media": "[]",
        "media_context": "",
        "conversation_id": conversation_id_str,
        "origen": "debounce",
    }


async def _campos_para_dlq(conversation_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, str]:
    """Reconstruye los campos de un entrante del stream a partir de la conversación."""
    from core.db.models import ChannelConnection, Conversation, Customer
    from core.db.session import get_session

    tenant_id = str(payload["tenant_id"])
    campos = _campos_minimos(str(conversation_id), payload)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None or str(conversation.tenant_id) != tenant_id:
                raise LookupError("conversación no encontrada")
            customer = await session.get(Customer, conversation.customer_id)
            connection = (
                await session.get(ChannelConnection, conversation.connection_id)
                if conversation.connection_id
                else None
            )
    channel = conversation.channel or "whatsapp"
    campos["channel"] = channel
    if connection is not None and str(connection.tenant_id) == tenant_id:
        campos["provider"] = connection.provider
        campos["connection_external_id"] = connection.external_id or ""
        campos["connection_id"] = str(connection.id)
    else:
        campos["provider"] = "web" if channel == "web" else "meta"
    if customer is not None and str(customer.tenant_id) == tenant_id:
        campos["customer_external_id"] = customer.external_id or ""
        campos["customer_name"] = customer.name or ""
    return campos


async def _fire_conversation(conversation_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Procesa el turno diferido. Un fallo se propaga: lo reintenta `_resolver_disparo` (H2)."""
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


async def recover_orphaned_conversations(*, periodico: bool = False) -> None:
    """Reprograma conversaciones con inbound pendiente sin timer vivo.

    Recorre los tenants uno a uno dentro de tenant_scope: la query es RLS-aware
    (ADR-0012), así que cada tenant recupera solo sus conversaciones.

    Al arrancar (`periodico=False`) no hay turnos en vuelo y se recupera todo, como siempre. El
    barrido periódico (H2) corre con turnos en vuelo, así que solo mira inbound más viejos que lo
    que puede durar un turno y más nuevos que `ORPHAN_MAX_AGE_S`, salta la conversación con un
    turno diferido en curso y recupera cada mensaje una sola vez (un mensaje que una política dejó
    sin respuesta a propósito no se re-dispara cada ciclo; si su turno falla, lo sigue el contador).
    """
    from sqlalchemy import text

    from core.admin.kill_switch_service import ultima_reactivacion
    from core.db.session import get_session
    from core.shared import kill_switch

    # Interruptor global (HU-ARCH-38, ADR-0200): activo (o ilegible) no se recupera nada; reactivado, no se
    # responde en lote lo que llegó antes: queda en la bandeja para una persona.
    if await kill_switch.global_activo():
        log.warning("orphan_recovery_omitida_kill_switch")
        return
    r = get_redis()
    try:
        desde = await ultima_reactivacion()
        # Ventana del barrido periódico. El piso va en la query (si el último mensaje es más viejo,
        # todos lo son); el techo se mira en Python sobre el ÚLTIMO mensaje: en la query tomaría
        # un inbound viejo de una conversación que ya se respondió.
        desde_ventana = (
            datetime.now(UTC) - timedelta(seconds=ORPHAN_MAX_AGE_S) if periodico else None
        )
        hasta_ventana = datetime.now(UTC) - timedelta(seconds=_orphan_min_age_s()) if periodico else None
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
                                       m.conversation_id, m.id, m.direction, m.content, m.created_at
                                FROM messages m
                                JOIN conversations c ON c.id = m.conversation_id
                                WHERE c.status = 'open' AND c.bot_paused = false
                                  AND m.tenant_id = :tid AND c.tenant_id = :tid
                                  AND (CAST(:desde AS timestamptz) IS NULL
                                       OR m.created_at > CAST(:desde AS timestamptz))
                                  AND (CAST(:desde_ventana AS timestamptz) IS NULL
                                       OR m.created_at > CAST(:desde_ventana AS timestamptz))
                                ORDER BY m.conversation_id, m.created_at DESC
                                """
                            ),
                            {"desde": desde, "tid": tenant_id, "desde_ventana": desde_ventana},
                        )
                    ).all()
                # DENTRO del tenant_scope (bug encontrado por auditor-multitenant,
                # 2026-09-12): `schedule_debounce` lee `get_current_tenant_id()`
                # para sembrar `tenant_id` en el payload -- afuera de este bloque
                # el ContextVar ya volvió a None y la recuperación quedaba
                # silenciosamente inoperante para TODOS los tenants (el payload
                # sin tenant_id hace que `_fire_conversation` descarte el turno).
                for conversation_id, message_id, direction, content, created_at in rows:
                    if direction != "inbound":
                        continue
                    if hasta_ventana is not None and created_at > hasta_ventana:
                        continue  # puede seguir en su ventana de debounce o en turno
                    has_timer = await r.zscore(PENDING_KEY, str(conversation_id))
                    if has_timer is not None:
                        continue
                    if periodico and not await _reclamar_recuperacion(
                        r, str(tenant_id), str(conversation_id), str(message_id)
                    ):
                        continue
                    await schedule_debounce(
                        uuid.UUID(str(conversation_id)),
                        0,
                        {"latest_message": content or "", "inbound_message_ids": [message_id]},
                    )
                    recovered += 1
        if recovered:
            log.info("orphaned_conversations_recovered", count=recovered, periodico=periodico)
    except Exception:
        log.exception("orphan_recovery_failed", periodico=periodico)


def _orphan_min_age_s() -> int:
    """Edad mínima del último inbound para el barrido periódico: ventana de debounce más larga
    (30 s) + lo que puede durar el turno diferido."""
    return _in_flight_ttl_s() + 30


async def _reclamar_recuperacion(r: Any, tenant_id: str, conversation_id: str, message_id: str) -> bool:
    """¿Le toca a este barrido recuperar el mensaje? No, si hay un turno diferido en curso o si
    ya se recuperó antes (SET NX: entre réplicas, una sola lo recupera)."""
    if await r.exists(IN_FLIGHT_KEY.format(tenant_id=tenant_id, conversation_id=conversation_id)):
        return False
    return bool(
        await r.set(
            RECOVERED_KEY.format(tenant_id=tenant_id, message_id=message_id),
            "1",
            nx=True,
            ex=2 * ORPHAN_MAX_AGE_S,
        )
    )

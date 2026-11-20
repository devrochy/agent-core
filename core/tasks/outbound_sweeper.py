"""Barrido de salientes atascadas (H3).

La salida es estrictamente en orden por conversación (`_is_earliest_queued` y `_enqueue_next` en
`core/tasks/outbound_send.py`): si se pierde la tarea Celery del primer outbound `queued` (falla
el `.delay`, el worker muere sin reentrega), todo lo que viene detrás en esa conversación queda
bloqueado para siempre. Este cron (cada 5 min, `core_cron`, ADR-0183) lo destraba:

1. Negocio por negocio, dentro de su `tenant_scope` (RLS forzada en `messages`), busca el primer
   outbound `queued` de cada conversación con más de `outbound_sweep_minutos`. Solo la cabeza:
   las demás las encadena ella al terminar.
2. Lo salta si tiene una tarea viva: claim en `outbound:claim:*` (se está enviando) o marca
   `outbound:programado:*` (espera un reintento con countdown o una pausa anti-baneo).
3. Si no, lo reencola con `outbound_send_task`. El claim de la tarea evita el doble envío si la
   original reaparece.
4. Tras `outbound_sweep_max_reencolados` reencolados sin que avance, lo marca `failed` con
   `_mark_failed` (que escala a una persona) y encadena el siguiente.

Falla cerrado: sin Redis no puede saber qué tiene tarea viva, así que esa pasada no hace nada.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.config import get_settings
from core.db.models import Message, Tenant
from core.db.session import get_session
from core.gateway.meta_errors import GraphError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.outbound_send import (
    _enqueue_next,
    _mark_failed,
    claim_key,
    outbound_send_task,
    programado_key,
)

log = get_logger(__name__)

LOTE = 200  # cabezas por negocio y pasada
_REENCOLADOS_PREFIX = "outbound:barrido:"
_REENCOLADOS_TTL_S = 24 * 3600
MOTIVO_AGOTADO = (
    "El mensaje quedó atascado en la cola de salida y no se pudo enviar tras varios intentos"
)


async def _cabezas_atascadas(tenant_id: uuid.UUID, limite: datetime) -> list[tuple[uuid.UUID, uuid.UUID, datetime]]:
    """(id, conversation_id, created_at) del primer outbound `queued` de cada conversación del
    negocio, si es anterior a `limite`. El más antiguo de la conversación siempre entra si alguno
    entra, así que DISTINCT ON sobre las filas filtradas da la cabeza real."""
    async with tenant_scope(tenant_id), get_session() as session:
        filas = await session.execute(
            select(Message.id, Message.conversation_id, Message.created_at)
            .where(
                Message.tenant_id == tenant_id,
                Message.direction == "outbound",
                Message.status == "queued",
                Message.created_at < limite,
            )
            .order_by(Message.conversation_id, Message.created_at, Message.id)
            .distinct(Message.conversation_id)
            .limit(LOTE)
        )
        return [(f.id, f.conversation_id, f.created_at) for f in filas]


async def _tiene_tarea_viva(redis, tenant_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID) -> bool:
    vivas = await redis.exists(
        claim_key(tenant_id, conversation_id, message_id), programado_key(tenant_id, message_id)
    )
    return bool(vivas)


async def _contar_reencolado(redis, tenant_id: uuid.UUID, message_id: uuid.UUID) -> int:
    clave = f"{_REENCOLADOS_PREFIX}{tenant_id}:{message_id}"
    total = int(await redis.incr(clave))
    if total == 1:
        await redis.expire(clave, _REENCOLADOS_TTL_S)
    return total


async def barrer_salientes_atascadas(ahora: datetime | None = None) -> dict[str, int]:
    """Una pasada del barrido. Devuelve los conteos (también van al log de resumen)."""
    from core.memory.working import get_redis

    settings = get_settings()
    ahora = ahora or datetime.now(UTC)
    limite = ahora - timedelta(minutes=settings.outbound_sweep_minutos)
    tope = settings.outbound_sweep_max_reencolados
    conteo = {"revisadas": 0, "con_tarea": 0, "reencoladas": 0, "fallidas": 0, "errores": 0}

    redis = get_redis()
    try:
        await redis.ping()
    except Exception as exc:
        log.error("outbound_sweeper_sin_redis", motivo=type(exc).__name__)
        conteo["errores"] += 1
        return conteo

    async with get_session() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))
        ).scalars().all()

    for tenant_id in tenant_ids:
        try:
            cabezas = await _cabezas_atascadas(tenant_id, limite)
        except Exception as exc:
            log.error("outbound_sweeper_consulta_fallida", tenant_id=str(tenant_id), motivo=type(exc).__name__)
            conteo["errores"] += 1
            continue
        for message_id, conversation_id, created_at in cabezas:
            conteo["revisadas"] += 1
            try:
                if await _tiene_tarea_viva(redis, tenant_id, conversation_id, message_id):
                    conteo["con_tarea"] += 1
                    continue
                intento = await _contar_reencolado(redis, tenant_id, message_id)
                edad_s = int((ahora - created_at).total_seconds())
                if intento > tope:
                    log.error(
                        "outbound_sweeper_agotado",
                        message_id=str(message_id),
                        tenant_id=str(tenant_id),
                        conversation_id=str(conversation_id),
                        edad_s=edad_s,
                        reencolados=intento - 1,
                    )
                    info = await _mark_failed(
                        message_id,
                        tenant_id,
                        GraphError(MOTIVO_AGOTADO, category="TRANSIENT", is_transient=True),
                        dlq=True,
                    )
                    conteo["fallidas"] += 1
                    if info is not None:
                        conv_id, creado, fallido = info
                        await _enqueue_next(conv_id, tenant_id, creado, fallido)
                    continue
                try:
                    outbound_send_task.delay(str(message_id), str(tenant_id))
                except Exception:
                    # Broker caído: no cuenta como intento (no hubo reencolado real).
                    await redis.decr(f"{_REENCOLADOS_PREFIX}{tenant_id}:{message_id}")
                    raise
                conteo["reencoladas"] += 1
                log.warning(
                    "outbound_sweeper_reencolado",
                    message_id=str(message_id),
                    tenant_id=str(tenant_id),
                    conversation_id=str(conversation_id),
                    edad_s=edad_s,
                    intento=intento,
                    motivo="queued_sin_tarea_viva",
                )
            except Exception as exc:
                log.error(
                    "outbound_sweeper_mensaje_fallido",
                    message_id=str(message_id),
                    tenant_id=str(tenant_id),
                    motivo=type(exc).__name__,
                )
                conteo["errores"] += 1

    log.info("outbound_sweeper_resumen", **conteo)
    return conteo

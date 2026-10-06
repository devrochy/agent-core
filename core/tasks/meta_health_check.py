"""Cron de salud completa (horario): corre los 7 checks de
`core/gateway/meta_health.py` por cada `MetaConnection` `connected`/
`degraded` (no `draft`/`revoked`/`error` -- no tiene sentido diagnosticar
algo que nunca se conectó o que ya se dio por perdido), persiste
`health`/`status`/`last_health_check_at`.

El check `app_subscription_active` (15 min) sigue siendo el cron YA
EXISTENTE (`meta_subscriptions_health.py`) -- acá solo se LEE su
resultado persistido, nunca se repite el `GET /{app_id}/subscriptions`.

También corre las dos señales globales que no son por-conexión: tamaño
del DLQ de entrada y el log de webhooks sin procesar hace más de 5 min
("worker caído").
"""

from __future__ import annotations

from core.db.models import MetaConnection
from core.db.session import get_session
from core.gateway.meta_health import decide_status, run_health_checks
from core.shared.alerts import send_alert
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_CONNECTIONS_TO_CHECK = ("connected", "degraded")


async def _connections_to_check() -> list[MetaConnection]:
    """Conexiones `connected`/`degraded` de TODOS los tenants.

    Enumera ids cross-tenant vía `admin_meta_connection_ids` (SECURITY DEFINER,
    migración 0033) y luego hidrata cada fila bajo el `tenant_scope` de su
    tenant -- un `select` plano sin scope devolvería 0 filas por RLS (el cron
    correría como no-op silencioso)."""
    from core.db.admin_enum import admin_meta_connection_ids

    out: list[MetaConnection] = []
    for connection_id, tenant_id in await admin_meta_connection_ids(list(_CONNECTIONS_TO_CHECK)):
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                connection = await session.get(MetaConnection, connection_id)
                if connection is not None:
                    out.append(connection)
    return out


async def _check_one(connection: MetaConnection) -> None:
    async with tenant_scope(connection.tenant_id):
        checks = await run_health_checks(connection, connection.tenant_id)
        new_status, reason = decide_status(checks)

        async with get_session() as session:
            db_connection = await session.get(MetaConnection, connection.id)
            if db_connection is None or db_connection.tenant_id != connection.tenant_id:
                return  # defensa en profundidad -- no debería pasar nunca
            if db_connection.status not in _CONNECTIONS_TO_CHECK or db_connection.deleted_at is not None:
                # Cambió durante la corrida (la desactivaron o la borraron): no se pisa. Con
                # `degraded`/`error` volvería a prestar su secreto (ADR-0101).
                return
            db_connection.health = checks
            db_connection.status = new_status
            db_connection.status_reason = reason
            from core.db.models import utcnow

            db_connection.last_health_check_at = utcnow()


async def check_all_connections_health() -> None:
    """Nunca levanta excepción: una conexión caída no debe interrumpir el
    chequeo de las demás (mismo criterio que `check_all_subscriptions_health`)."""
    for connection in await _connections_to_check():
        try:
            await _check_one(connection)
        except Exception:
            log.exception("meta_health_check_error_inesperado", connection_id=str(connection.id))

    await _check_global_signals()


async def _check_global_signals() -> None:
    from core.gateway.webhooks import count_stalled_webhook_events
    from core.orchestrator.consumer import DEAD_LETTER_STREAM

    try:
        stalled = await count_stalled_webhook_events()
        if stalled > 0:
            await send_alert(
                "Webhooks sin procesar",
                f"{stalled} eventos de webhook sin procesar hace más de 5 min -- posible worker caído.",
                severity="critical",
            )
    except Exception:
        log.exception("meta_health_check_stalled_events_fallo")

    try:
        from core.memory.working import get_redis

        redis = get_redis()
        dlq_size = await redis.xlen(DEAD_LETTER_STREAM)
        if dlq_size > 0:
            await send_alert(
                "DLQ de entrada con eventos pendientes",
                f"{dlq_size} eventos en {DEAD_LETTER_STREAM} -- aviso al on-call.",
                severity="critical",
            )
    except Exception:
        log.exception("meta_health_check_dlq_size_fallo")

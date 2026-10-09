"""Aplicación Celery (broker y backend: Redis).

El worker y el beat importan este módulo. Al arrancar, el worker escanea los
plugins para que sus tareas @register_cron queden registradas.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import after_setup_logger, after_setup_task_logger, beat_init, worker_init

from core.config import get_settings
from core.shared.async_bridge import run_async
from core.shared.logger import get_logger, silenciar_urls_de_clientes_http

log = get_logger(__name__)
settings = get_settings()

celery = Celery(
    "agent_core",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # `include` (no un import suelto): garantiza que el worker registre las
    # tasks definidas en estos módulos al finalizar el arranque. Sin esto,
    # `core.outbound_send` quedaba como "unregistered task" y los mensajes de
    # salida nunca se enviaban.
    include=["core.tasks.outbound_send", "core.tasks.push_tasks"],
)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_acks_late=True,          # el mensaje se re-entrega si el worker muere
    task_reject_on_worker_lost=True,
    task_time_limit=300,
    beat_schedule={},             # lo llenan los @register_cron de los plugins
)


# ---------------------------------------------------------------- cron de core
# Salud de suscripciones de webhook de Meta (cada 15 min): capacidad de
# CORE, no de un pack -- no pasa por `register_cron` (core/tasks/decorators.py),
# que está atado a `plugin_manager.is_active(plugin_id)` y no tiene sentido
# para algo que no es un plugin. Se registra directo acá.
_META_SUBSCRIPTIONS_HEALTH_TASK = "core.meta_subscriptions_health_check"


@celery.task(name=_META_SUBSCRIPTIONS_HEALTH_TASK)
def _meta_subscriptions_health_check() -> None:
    from core.tasks.meta_subscriptions_health import check_all_subscriptions_health

    try:
        run_async(check_all_subscriptions_health())
    except Exception:
        log.exception("cron_task_failed", task=_META_SUBSCRIPTIONS_HEALTH_TASK)


celery.conf.beat_schedule[_META_SUBSCRIPTIONS_HEALTH_TASK] = {
    "task": _META_SUBSCRIPTIONS_HEALTH_TASK,
    "schedule": crontab(minute="*/15"),
}

# Sync de plantillas de WhatsApp (cada hora): el webhook `message_template_
# status_update` es la fuente de verdad real, esto es solo el fallback.
_MESSAGE_TEMPLATE_SYNC_TASK = "core.message_template_sync"


@celery.task(name=_MESSAGE_TEMPLATE_SYNC_TASK)
def _message_template_sync() -> None:
    from core.tasks.message_template_sync import sync_all_waba_templates

    try:
        run_async(sync_all_waba_templates())
    except Exception:
        log.exception("cron_task_failed", task=_MESSAGE_TEMPLATE_SYNC_TASK)


celery.conf.beat_schedule[_MESSAGE_TEMPLATE_SYNC_TASK] = {
    "task": _MESSAGE_TEMPLATE_SYNC_TASK,
    "schedule": crontab(minute="0"),
}

# Sync de `pricing_analytics` (diario, de madrugada): el webhook `statuses`
# (con su objeto `pricing`) llega en tiempo real pero NUNCA trae un monto
# en dólares -- esto es lo único que sincroniza el `cost` autoritativo.
_META_PRICING_SYNC_TASK = "core.meta_pricing_sync"


@celery.task(name=_META_PRICING_SYNC_TASK)
def _meta_pricing_sync() -> None:
    from core.tasks.meta_pricing_sync import sync_all_waba_pricing

    try:
        run_async(sync_all_waba_pricing())
    except Exception:
        log.exception("cron_task_failed", task=_META_PRICING_SYNC_TASK)


celery.conf.beat_schedule[_META_PRICING_SYNC_TASK] = {
    "task": _META_PRICING_SYNC_TASK,
    "schedule": crontab(hour="2", minute="0"),
}

# Salud completa de conexiones Meta (horaria) -- `app_subscription_active`
# (15 min) sigue siendo `core.meta_subscriptions_health_check`, este cron
# solo LEE ese resultado, nunca repite el GET.
_META_HEALTH_CHECK_TASK = "core.meta_health_check"


@celery.task(name=_META_HEALTH_CHECK_TASK)
def _meta_health_check() -> None:
    from core.tasks.meta_health_check import check_all_connections_health

    try:
        run_async(check_all_connections_health())
    except Exception:
        log.exception("cron_task_failed", task=_META_HEALTH_CHECK_TASK)


celery.conf.beat_schedule[_META_HEALTH_CHECK_TASK] = {
    "task": _META_HEALTH_CHECK_TASK,
    "schedule": crontab(minute="30"),
}

# Alerta de access tokens de Meta vencidos/por vencer (diaria, de madrugada).
_META_TOKEN_EXPIRY_TASK = "core.meta_token_expiry_check"


@celery.task(name=_META_TOKEN_EXPIRY_TASK)
def _meta_token_expiry_check() -> None:
    from core.tasks.meta_token_expiry_check import check_tokens_expiry

    try:
        run_async(check_tokens_expiry())
    except Exception:
        log.exception("cron_task_failed", task=_META_TOKEN_EXPIRY_TASK)


celery.conf.beat_schedule[_META_TOKEN_EXPIRY_TASK] = {
    "task": _META_TOKEN_EXPIRY_TASK,
    "schedule": crontab(hour="3", minute="15"),
}

# Retención de la memoria semántica (ADR-0053; no-op si MEMORY_RETENTION_DAYS=0).
_MEMORY_RETENTION_TASK = "core.memory_retention"


@celery.task(name=_MEMORY_RETENTION_TASK)
def _memory_retention() -> None:
    from core.tasks.memory_retention import purge_expired_memory

    try:
        run_async(purge_expired_memory())
    except Exception:
        log.exception("cron_task_failed", task=_MEMORY_RETENTION_TASK)


celery.conf.beat_schedule[_MEMORY_RETENTION_TASK] = {
    "task": _MEMORY_RETENTION_TASK,
    "schedule": crontab(hour="3", minute="45"),
}

# Heartbeat del worker (cada 1 min) -- la única forma de que el checklist
# de promoción a producción (core/gateway/meta_production_readiness.py::
# check_worker_heartbeat_and_dlq) sepa que el worker de Celery está vivo,
# no solo que existe la cola.
_WORKER_HEARTBEAT_TASK = "core.worker_heartbeat"
_WORKER_HEARTBEAT_KEY = "worker:heartbeat"
_WORKER_HEARTBEAT_TTL_S = 180


@celery.task(name=_WORKER_HEARTBEAT_TASK)
def _worker_heartbeat() -> None:
    async def _set_heartbeat() -> None:
        from datetime import UTC, datetime

        from core.memory.working import get_redis

        redis = get_redis()
        await redis.set(_WORKER_HEARTBEAT_KEY, datetime.now(UTC).isoformat(), ex=_WORKER_HEARTBEAT_TTL_S)

    try:
        run_async(_set_heartbeat())
    except Exception:
        log.exception("cron_task_failed", task=_WORKER_HEARTBEAT_TASK)


celery.conf.beat_schedule[_WORKER_HEARTBEAT_TASK] = {
    "task": _WORKER_HEARTBEAT_TASK,
    "schedule": crontab(minute="*/1"),
}

# Normalización de suscripciones vencidas (diaria): pasa `trialing`->`expired` y
# `active`->`past_due` para que el panel refleje el estado real (ADR-0045). El
# bloqueo efectivo NO depende de este cron (se evalúa en vivo en webhook/consumer).
_BILLING_LIFECYCLE_TASK = "core.billing_lifecycle"


@celery.task(name=_BILLING_LIFECYCLE_TASK)
def _billing_lifecycle() -> None:
    from core.tasks.billing_lifecycle import normalize_expired_subscriptions

    try:
        run_async(normalize_expired_subscriptions())
    except Exception:
        log.exception("cron_task_failed", task=_BILLING_LIFECYCLE_TASK)


celery.conf.beat_schedule[_BILLING_LIFECYCLE_TASK] = {
    "task": _BILLING_LIFECYCLE_TASK,
    "schedule": crontab(hour="4", minute="0"),
}

# Renovación del plan de la plataforma (HU-SH-04, ADR-0123): el cobro del período siguiente
# BILLING_DIAS_RENOVACION días antes, con aviso por el canal preferido. A las 13:00 UTC
# (08:00 en Bogotá), para que el aviso llegue de mañana.
_BILLING_RENOVACION_TASK = "core.billing_renovacion"


@celery.task(name=_BILLING_RENOVACION_TASK)
def _billing_renovacion() -> None:
    from core.billing.renovacion import renovar_suscripciones

    try:
        run_async(renovar_suscripciones())
    except Exception:
        log.exception("cron_task_failed", task=_BILLING_RENOVACION_TASK)


celery.conf.beat_schedule[_BILLING_RENOVACION_TASK] = {
    "task": _BILLING_RENOVACION_TASK,
    "schedule": crontab(hour="13", minute="0"),
}

# Conciliación diaria de los dos canales del cobro de la plataforma (HU-SH-04): compara el
# asistente con el negocio y alerta cada diferencia con su detalle.
_BILLING_CONCILIACION_TASK = "core.billing_conciliacion"


@celery.task(name=_BILLING_CONCILIACION_TASK)
def _billing_conciliacion() -> None:
    from core.billing.conciliacion import conciliar

    try:
        run_async(conciliar())
    except Exception:
        log.exception("cron_task_failed", task=_BILLING_CONCILIACION_TASK)


celery.conf.beat_schedule[_BILLING_CONCILIACION_TASK] = {
    "task": _BILLING_CONCILIACION_TASK,
    "schedule": crontab(hour="5", minute="30"),
}

# Despachador del outbox de dominio (HU-SUB-03, ADR-0107): cada 30 s entrega
# los `domain_events` pendientes vencidos con backoff y DLQ.
_DOMAIN_EVENTS_DISPATCH_TASK = "core.domain_events_dispatch"


@celery.task(name=_DOMAIN_EVENTS_DISPATCH_TASK)
def _domain_events_dispatch() -> None:
    from core.tasks.domain_events_dispatch import dispatch_domain_events_task

    try:
        dispatch_domain_events_task()
    except Exception:
        log.exception("cron_task_failed", task=_DOMAIN_EVENTS_DISPATCH_TASK)


celery.conf.beat_schedule[_DOMAIN_EVENTS_DISPATCH_TASK] = {
    "task": _DOMAIN_EVENTS_DISPATCH_TASK,
    "schedule": 30.0,
}


def _load_plugins() -> None:
    """Carga los plugins (entry_points) e importa los jobs para que corran los
    `@register_cron` de los packs. Necesario tanto en el worker como en el beat."""
    from core.observability.sentry_setup import configure_sentry
    from core.plugin_manager import plugin_manager

    configure_sentry()  # no-op sin SENTRY_ENABLED/SENTRY_DSN -- el worker también reporta
    plugin_manager.scan()
    # Importa los módulos de jobs para que los decoradores corran
    import core.tasks.decorators  # noqa: F401


@after_setup_logger.connect
@after_setup_task_logger.connect
def _silenciar_urls_al_configurar_logs(**_kwargs: object) -> None:
    """El worker y el beat configuran sus logs con Celery, no con `configure_logging`:
    sin esto, `httpx` registraba a nivel INFO la URL de `/oauth/access_token` con el
    App Secret en la query (visto en el worker de test, 2026-10-08)."""
    silenciar_urls_de_clientes_http()


@worker_init.connect
def _load_plugins_on_worker_start(**_kwargs: object) -> None:
    """El worker necesita los plugins cargados para resolver sus tareas."""
    _load_plugins()


@beat_init.connect
def _load_plugins_on_beat_start(sender: object = None, **_kwargs: object) -> None:
    """El beat NO ejecuta `worker_init`: sin este escaneo, los `@register_cron`
    de los packs nunca entran al `beat_schedule` y sus crons (p. ej. los
    follow-ups de pack-commerce) no se programan.

    Celery crea el planificador (y le copia `beat_schedule`) ANTES de emitir
    `beat_init`, así que lo que registran los packs aquí hay que volver a
    pasárselo al planificador ya creado. Sin eso, ningún cron de pack se
    despachaba (visto al verificar HU-SUB-09 en test: el beat arrancaba a las
    16:09:23 y los crons se registraban 16:09:24, sin un solo envío después).
    """
    _load_plugins()
    scheduler = getattr(sender, "scheduler", None)
    if scheduler is not None:
        scheduler.merge_inplace(celery.conf.beat_schedule)
        log.info("beat_schedule_cargado", tareas=sorted(scheduler.schedule))

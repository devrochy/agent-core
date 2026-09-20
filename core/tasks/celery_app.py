"""Aplicación Celery (broker y backend: Redis).

El worker y el beat importan este módulo. Al arrancar, el worker escanea los
plugins para que sus tareas @register_cron queden registradas.
"""

from __future__ import annotations

import asyncio

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_init

from core.config import get_settings
from core.shared.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

celery = Celery(
    "agent_core",
    broker=settings.redis_url,
    backend=settings.redis_url,
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
        asyncio.run(check_all_subscriptions_health())
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
        asyncio.run(sync_all_waba_templates())
    except Exception:
        log.exception("cron_task_failed", task=_MESSAGE_TEMPLATE_SYNC_TASK)


celery.conf.beat_schedule[_MESSAGE_TEMPLATE_SYNC_TASK] = {
    "task": _MESSAGE_TEMPLATE_SYNC_TASK,
    "schedule": crontab(minute="0"),
}


@worker_init.connect
def _load_plugins_on_worker_start(**_kwargs: object) -> None:
    """El worker necesita los plugins cargados para resolver sus tareas."""
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    # Importa los módulos de jobs para que los decoradores corran
    import core.tasks.decorators  # noqa: F401

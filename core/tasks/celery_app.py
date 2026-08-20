"""Aplicación Celery (broker y backend: Redis).

El worker y el beat importan este módulo. Al arrancar, el worker escanea los
plugins para que sus tareas @register_cron queden registradas.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_init

from core.config import get_settings

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


@worker_init.connect
def _load_plugins_on_worker_start(**_kwargs: object) -> None:
    """El worker necesita los plugins cargados para resolver sus tareas."""
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    # Importa los módulos de jobs para que los decoradores corran
    import core.tasks.decorators  # noqa: F401

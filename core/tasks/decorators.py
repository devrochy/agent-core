"""Registro de tareas programadas por plugins.

Uso en un plugin (ver docs/PLUGIN-CONTRACT.md § Cron):

    from core.tasks.decorators import register_cron

    @register_cron("mod_crm_engine", "0 9 * * *")   # todos los días 9:00 UTC
    async def follow_up_leads() -> None:
        ...

El decorador:
1. Envuelve la corrutina en una tarea Celery síncrona (asyncio.run).
2. La agrega al beat_schedule con el crontab dado.
3. La tarea NO corre si el plugin está inactivo (guard automático).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from celery.schedules import crontab

from core.shared.logger import get_logger
from core.tasks.celery_app import celery

log = get_logger(__name__)

AsyncJob = Callable[[], Awaitable[None]]


def _parse_crontab(expression: str) -> crontab:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(f"Expresión cron inválida: '{expression}' (se esperan 5 campos)")
    minute, hour, day_of_month, month, day_of_week = parts
    return crontab(
        minute=minute, hour=hour, day_of_month=day_of_month,
        month_of_year=month, day_of_week=day_of_week,
    )


def register_cron(plugin_id: str, cron_expression: str) -> Callable[[AsyncJob], AsyncJob]:
    def decorator(fn: AsyncJob) -> AsyncJob:
        task_name = f"{plugin_id}.{fn.__name__}"

        @celery.task(name=task_name)
        def _celery_task() -> None:
            from core.plugin_manager import plugin_manager

            if not plugin_manager.is_active(plugin_id):
                log.info("cron_skipped_plugin_inactive", plugin=plugin_id, task=task_name)
                return
            try:
                asyncio.run(fn())
            except Exception:
                log.exception("cron_task_failed", plugin=plugin_id, task=task_name)

        celery.conf.beat_schedule[task_name] = {
            "task": task_name,
            "schedule": _parse_crontab(cron_expression),
        }
        log.info("cron_registered", plugin=plugin_id, task=task_name, cron=cron_expression)
        return fn

    return decorator


def enqueue(task_name: str, *args: Any, **kwargs: Any) -> None:
    """Encola una tarea puntual (no programada) para ejecución en el worker."""
    celery.send_task(task_name, args=args, kwargs=kwargs)

"""Registro de tareas programadas por plugins.

Uso en un plugin (ver docs/PLUGIN-CONTRACT.md § Cron):

    from core.tasks.decorators import register_cron

    @register_cron("mod_crm_engine", "0 9 * * *")   # todos los días 9:00 UTC
    async def follow_up_leads() -> None:
        ...

El decorador:
1. Envuelve la corrutina en una tarea Celery síncrona (`run_async`, loop persistente).
2. La agrega al beat_schedule con el crontab dado.
3. La tarea NO corre si el plugin está inactivo (guard automático).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from celery.schedules import crontab

from core.shared.async_bridge import run_async
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

            # El worker no pasa por el lifespan de la API: sin esto `_active` está
            # vacío en el proceso y TODO cron de pack se salteaba (visto al hacer
            # HU-SUB-04; mismo origen que el arreglo del outbox, ADR-0107).
            try:
                run_async(plugin_manager.ensure_process_activation())
            except Exception:
                log.exception("cron_activation_failed", plugin=plugin_id, task=task_name)
            if not plugin_manager.is_active(plugin_id):
                log.info("cron_skipped_plugin_inactive", plugin=plugin_id, task=task_name)
                return
            try:
                run_async(fn())
            except Exception:
                log.exception("cron_task_failed", plugin=plugin_id, task=task_name)

        celery.conf.beat_schedule[task_name] = {
            "task": task_name,
            "schedule": _parse_crontab(cron_expression),
        }
        log.info("cron_registered", plugin=plugin_id, task=task_name, cron=cron_expression)
        return fn

    return decorator


AsyncTask = Callable[..., Awaitable[None]]


def register_task(plugin_id: str) -> Callable[[AsyncTask], AsyncTask]:
    """Tarea puntual de un pack (no programada), encolada con `core.tasks.queue.task_queue`.

    Mismo envoltorio que `register_cron`: nombre `<plugin_id>.<función>`, activación del proceso y
    guardia de pack activo. Recibe solo kwargs JSON (ids, nunca datos del negocio: el broker es
    Redis). La tarea fija su propio `tenant_scope` desde sus argumentos. Un error se registra y no
    se reintenta: la tarea deja su estado en la base (p. ej. un documento `fallido`).
    """

    def decorator(fn: AsyncTask) -> AsyncTask:
        task_name = f"{plugin_id}.{fn.__name__}"

        @celery.task(name=task_name)
        def _celery_task(**kwargs: Any) -> None:
            from core.plugin_manager import plugin_manager

            try:
                run_async(plugin_manager.ensure_process_activation())
            except Exception:
                log.exception("task_activation_failed", plugin=plugin_id, task=task_name)
            if not plugin_manager.is_active(plugin_id):
                log.info("task_skipped_plugin_inactive", plugin=plugin_id, task=task_name)
                return
            try:
                run_async(fn(**kwargs))
            except Exception as exc:
                # Solo la clase: un error de SQLAlchemy trae los parámetros de la consulta, que
                # pueden ser datos del negocio (p. ej. el texto de un documento).
                log.error("task_failed", plugin=plugin_id, task=task_name, error=type(exc).__name__)

        log.info("task_registered", plugin=plugin_id, task=task_name)
        return fn

    return decorator


def enqueue(task_name: str, *args: Any, **kwargs: Any) -> None:
    """Encola una tarea puntual (no programada) para ejecución en el worker."""
    celery.send_task(task_name, args=args, kwargs=kwargs)

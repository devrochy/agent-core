"""TaskQueue: abstracción sobre el motor de colas.

Los plugins usan ESTA interfaz, nunca Celery directamente. Cambiar Celery por
Arq/RQ = escribir otro backend aquí, sin tocar ningún plugin
(ver docs/SDD-TECH-STACK.md § Portabilidad: Celery -> BullMQ en TypeScript).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TaskQueue(ABC):
    @abstractmethod
    def enqueue(self, task_name: str, **kwargs: Any) -> None: ...


class CeleryTaskQueue(TaskQueue):
    def enqueue(self, task_name: str, **kwargs: Any) -> None:
        from core.tasks.celery_app import celery

        celery.send_task(task_name, kwargs=kwargs)


task_queue: TaskQueue = CeleryTaskQueue()

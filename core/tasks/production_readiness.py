"""Jobs del checklist de producción (HU-CONN-07).

El request de la API solo registra la corrida y encola uno de estos jobs; el worker la
ejecuta y guarda el resultado con fecha (`core/admin/production_readiness_service.py`). Así
la revisión (varias llamadas a Meta, consultas al catálogo de Postgres, lectura de archivos)
nunca bloquea el event loop de la API, y no hay pytest ni subprocesos en ningún lado.

Patrón de `push_tasks`: task síncrono + `run_async` (loop persistente del worker). Sin
reintentos: la revisión es de solo lectura y el usuario la repite desde el panel; un fallo
deja la corrida en `failed` con un mensaje legible.
"""

from __future__ import annotations

import uuid

from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.tasks.celery_app import celery

log = get_logger(__name__)

CONNECTION_TASK = "core.meta_connection_production_check"
PLATFORM_TASK = "core.platform_readiness_check"


@celery.task(name=CONNECTION_TASK)
def run_connection_production_task(run_id: str, tenant_id: str) -> None:
    from core.admin.production_readiness_service import execute_connection_run

    try:
        run_async(execute_connection_run(uuid.UUID(run_id), uuid.UUID(tenant_id)))
    except Exception:
        log.exception("task_failed", task=CONNECTION_TASK, run_id=run_id, tenant_id=tenant_id)


@celery.task(name=PLATFORM_TASK)
def run_platform_readiness_task(run_id: str) -> None:
    from core.admin.production_readiness_service import execute_platform_run

    try:
        run_async(execute_platform_run(uuid.UUID(run_id)))
    except Exception:
        log.exception("task_failed", task=PLATFORM_TASK, run_id=run_id)

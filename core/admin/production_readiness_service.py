"""Checklist de producción (HU-CONN-07): pedir una corrida, leer la última y ejecutarla.

Dos alcances, cada uno con su tabla:

- **Conexión** (`meta_production_checks`, por negocio con RLS): lo pide el negocio desde
  "Producción" de una conexión; se evalúa con las credenciales de esa conexión.
- **Plataforma** (`platform_readiness_runs`, tabla de sistema): la app de Meta de la
  plataforma y la infraestructura; solo el master, desde "Plataforma → App de Meta". Quien
  llama (las rutas master-only de `core/api/routers/platform.py`) ya verificó el rol.

El request solo registra la corrida (`queued`) y la encola; el worker de Celery
(`core/tasks/production_readiness.py`) la ejecuta y guarda el resultado con fecha. Así una
revisión lenta de Meta o de la infraestructura nunca bloquea el event loop de la API.

Una sola corrida activa (`queued`/`running`) por conexión y una para la plataforma (índices
únicos parciales): pedir otra mientras corre devuelve la que está en curso. Una corrida activa
de más de `STALE_AFTER` se da por perdida (worker caído) y deja pedir otra.

Único punto de escritura de las dos tablas.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.db.models import MetaConnection, MetaProductionCheck, PlatformReadinessRun, utcnow
from core.db.session import get_owned, get_session
from core.gateway.meta_preflight import PreflightCheck
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACTIVE_STATUSES = ("queued", "running")
# El job tiene `task_time_limit=300`: pasado el doble, la corrida ya no va a terminar.
STALE_AFTER = timedelta(minutes=10)
STALE_ERROR = "La revisión no terminó a tiempo (el worker no respondió). Vuelve a intentarlo."
ENQUEUE_ERROR = "No se pudo encolar la revisión. Vuelve a intentarlo en unos minutos."
INTERNAL_ERROR = "La revisión falló por un error interno. Vuelve a intentarlo."
CONNECTION_GONE_ERROR = "La conexión ya no existe."

# Conexiones que cuentan para el paso a producción del negocio: ni desactivadas, ni sin alta.
_SIN_PRODUCCION = ("revoked", "draft")


class ConnectionNotFound(Exception):
    """La conexión no existe, es de otro negocio o está borrada."""


class EnqueueFailed(Exception):
    """El broker no aceptó el job: la corrida queda en `failed`."""


def serialize_checks(checks: Sequence[PreflightCheck], labels: dict[str, str]) -> list[dict[str, Any]]:
    """Ítems para guardar y mostrar: con su texto para el panel y la fecha de cada check."""
    return [
        {
            "key": check.key,
            "label": labels.get(check.key, check.key),
            "ok": check.ok,
            "detail": check.detail,
            "repair_action": check.repair_action,
            "checked_at": check.checked_at.isoformat() if check.checked_at else None,
        }
        for check in checks
    ]


def effective_status(status: str, requested_at: datetime | None, error: str | None) -> tuple[str, str | None]:
    """Estado para mostrar: una corrida activa vieja se muestra como fallida (sin escribir)."""
    if status in ACTIVE_STATUSES and requested_at is not None and utcnow() - requested_at > STALE_AFTER:
        return "failed", STALE_ERROR
    return status, error


def _ready(checks: Sequence[PreflightCheck]) -> bool:
    return bool(checks) and all(check.ok is True for check in checks)


async def _enqueue(task: Any, *args: str) -> None:
    """`delay` habla con Redis de forma síncrona: va a un hilo para no frenar el event loop."""
    await asyncio.to_thread(task.delay, *args)


# ------------------------------------------------------------------------- conexión
async def owned_connection(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> MetaConnection | None:
    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, connection_id, tenant_id)
    if connection is None or connection.deleted_at is not None:
        return None
    return connection


async def latest_connection_run(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> MetaProductionCheck | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(MetaProductionCheck)
                .where(
                    MetaProductionCheck.tenant_id == tenant_id,
                    MetaProductionCheck.connection_id == connection_id,
                    MetaProductionCheck.deleted_at.is_(None),
                )
                .order_by(MetaProductionCheck.requested_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _active_connection_run(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> MetaProductionCheck | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(MetaProductionCheck).where(
                    MetaProductionCheck.tenant_id == tenant_id,
                    MetaProductionCheck.connection_id == connection_id,
                    MetaProductionCheck.status.in_(ACTIVE_STATUSES),
                    MetaProductionCheck.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def request_connection_run(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, admin_id: uuid.UUID | None
) -> tuple[MetaProductionCheck, bool]:
    """Registra y encola una corrida para la conexión. Devuelve `(corrida, creada)`: si ya
    había una en curso, esa misma y `False`."""
    from core.tasks.production_readiness import run_connection_production_task

    if await owned_connection(tenant_id, connection_id) is None:
        raise ConnectionNotFound(str(connection_id))

    try:
        async with get_session() as session:
            await session.execute(
                update(MetaProductionCheck)
                .where(
                    MetaProductionCheck.tenant_id == tenant_id,
                    MetaProductionCheck.connection_id == connection_id,
                    MetaProductionCheck.status.in_(ACTIVE_STATUSES),
                    MetaProductionCheck.requested_at < utcnow() - STALE_AFTER,
                )
                .values(status="failed", error=STALE_ERROR, finished_at=utcnow())
            )
            active = (
                await session.execute(
                    select(MetaProductionCheck).where(
                        MetaProductionCheck.tenant_id == tenant_id,
                        MetaProductionCheck.connection_id == connection_id,
                        MetaProductionCheck.status.in_(ACTIVE_STATUSES),
                        MetaProductionCheck.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if active is not None:
                return active, False
            run = MetaProductionCheck(
                tenant_id=tenant_id, connection_id=connection_id, status="queued", requested_by=admin_id,
                requested_at=utcnow(), checks=[],
            )
            session.add(run)
            await session.flush()
    except IntegrityError:
        # Otro request la creó entre el SELECT y el INSERT: se devuelve esa.
        active = await _active_connection_run(tenant_id, connection_id)
        if active is None:
            raise
        return active, False

    try:
        await _enqueue(run_connection_production_task, str(run.id), str(tenant_id))
    except Exception as exc:
        log.exception("production_check_enqueue_failed", run_id=str(run.id), tenant_id=str(tenant_id))
        await _finish_connection_run(tenant_id, run.id, status="failed", error=ENQUEUE_ERROR)
        raise EnqueueFailed(ENQUEUE_ERROR) from exc
    log.info(
        "production_check_requested", run_id=str(run.id), tenant_id=str(tenant_id), connection_id=str(connection_id)
    )
    return run, True


async def _finish_connection_run(
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    ready: bool | None = None,
    checks: list[dict[str, Any]] | None = None,
) -> bool:
    """Cierra la corrida si sigue activa (una vencida mientras tanto no se pisa)."""
    values: dict[str, Any] = {"status": status, "error": error, "ready": ready, "finished_at": utcnow()}
    if checks is not None:
        values["checks"] = checks
    async with get_session() as session:
        result = await session.execute(
            update(MetaProductionCheck)
            .where(
                MetaProductionCheck.id == run_id,
                MetaProductionCheck.tenant_id == tenant_id,
                MetaProductionCheck.status.in_(ACTIVE_STATUSES),
            )
            .values(**values)
            .returning(MetaProductionCheck.id)
        )
        return result.scalar_one_or_none() is not None


async def execute_connection_run(run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Lo corre el worker: evalúa la conexión con sus credenciales y guarda el resultado."""
    from core.gateway.meta_production_readiness import (
        CONNECTION_ITEM_LABELS,
        run_connection_production_checks,
    )

    started = time.monotonic()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            run = (
                await session.execute(
                    update(MetaProductionCheck)
                    .where(
                        MetaProductionCheck.id == run_id,
                        MetaProductionCheck.tenant_id == tenant_id,
                        MetaProductionCheck.status.in_(ACTIVE_STATUSES),
                    )
                    .values(status="running", started_at=utcnow())
                    .returning(MetaProductionCheck.connection_id)
                )
            ).scalar_one_or_none()
        if run is None:
            log.info("production_check_skipped", run_id=str(run_id), tenant_id=str(tenant_id))
            return
        connection_id = run

        connection = await owned_connection(tenant_id, connection_id)
        if connection is None:
            await _finish_connection_run(tenant_id, run_id, status="failed", error=CONNECTION_GONE_ERROR)
            return
        try:
            checks = await run_connection_production_checks(connection, tenant_id)
        except Exception:
            log.exception("production_check_failed", run_id=str(run_id), tenant_id=str(tenant_id))
            await _finish_connection_run(tenant_id, run_id, status="failed", error=INTERNAL_ERROR)
            return
        ready = _ready(checks)
        saved = await _finish_connection_run(
            tenant_id, run_id, status="done", ready=ready, checks=serialize_checks(checks, CONNECTION_ITEM_LABELS)
        )
    log.info(
        "production_check_finished",
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        connection_id=str(connection_id),
        ready=ready,
        failing=[c.key for c in checks if c.ok is False],
        saved=saved,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def live_connection_ids(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Conexiones de Meta del negocio que cuentan para producción."""
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(MetaConnection.id)
                    .where(
                        MetaConnection.tenant_id == tenant_id,
                        MetaConnection.status.not_in(_SIN_PRODUCCION),
                        MetaConnection.deleted_at.is_(None),
                    )
                    .order_by(MetaConnection.created_at)
                )
            ).scalars().all()
        )


# ------------------------------------------------------------------------ plataforma
async def latest_platform_run() -> PlatformReadinessRun | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(PlatformReadinessRun).order_by(PlatformReadinessRun.requested_at.desc()).limit(1)
            )
        ).scalar_one_or_none()


async def _active_platform_run() -> PlatformReadinessRun | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(PlatformReadinessRun).where(PlatformReadinessRun.status.in_(ACTIVE_STATUSES))
            )
        ).scalar_one_or_none()


async def request_platform_run(admin_id: uuid.UUID | None) -> tuple[PlatformReadinessRun, bool]:
    """Registra y encola una corrida de la plataforma. Solo para el master (lo exige la ruta)."""
    from core.tasks.production_readiness import run_platform_readiness_task

    try:
        async with get_session() as session:
            await session.execute(
                update(PlatformReadinessRun)
                .where(
                    PlatformReadinessRun.status.in_(ACTIVE_STATUSES),
                    PlatformReadinessRun.requested_at < utcnow() - STALE_AFTER,
                )
                .values(status="failed", error=STALE_ERROR, finished_at=utcnow())
            )
            active = (
                await session.execute(
                    select(PlatformReadinessRun).where(PlatformReadinessRun.status.in_(ACTIVE_STATUSES))
                )
            ).scalar_one_or_none()
            if active is not None:
                return active, False
            run = PlatformReadinessRun(status="queued", requested_by=admin_id, requested_at=utcnow(), checks=[])
            session.add(run)
            await session.flush()
    except IntegrityError:
        active = await _active_platform_run()
        if active is None:
            raise
        return active, False

    try:
        await _enqueue(run_platform_readiness_task, str(run.id))
    except Exception as exc:
        log.exception("platform_readiness_enqueue_failed", run_id=str(run.id))
        await _finish_platform_run(run.id, status="failed", error=ENQUEUE_ERROR)
        raise EnqueueFailed(ENQUEUE_ERROR) from exc
    log.info("platform_readiness_requested", run_id=str(run.id))
    return run, True


async def _finish_platform_run(
    run_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    ready: bool | None = None,
    checks: list[dict[str, Any]] | None = None,
) -> bool:
    values: dict[str, Any] = {"status": status, "error": error, "ready": ready, "finished_at": utcnow()}
    if checks is not None:
        values["checks"] = checks
    async with get_session() as session:
        result = await session.execute(
            update(PlatformReadinessRun)
            .where(PlatformReadinessRun.id == run_id, PlatformReadinessRun.status.in_(ACTIVE_STATUSES))
            .values(**values)
            .returning(PlatformReadinessRun.id)
        )
        return result.scalar_one_or_none() is not None


async def execute_platform_run(run_id: uuid.UUID) -> None:
    """Lo corre el worker: los checks de la app de la plataforma e infraestructura."""
    from core.gateway.meta_production_readiness import PLATFORM_CHECK_LABELS, run_platform_checks

    started = time.monotonic()
    async with get_session() as session:
        claimed = (
            await session.execute(
                update(PlatformReadinessRun)
                .where(PlatformReadinessRun.id == run_id, PlatformReadinessRun.status.in_(ACTIVE_STATUSES))
                .values(status="running", started_at=utcnow())
                .returning(PlatformReadinessRun.id)
            )
        ).scalar_one_or_none()
    if claimed is None:
        log.info("platform_readiness_skipped", run_id=str(run_id))
        return
    try:
        checks = await run_platform_checks()
    except Exception:
        log.exception("platform_readiness_failed", run_id=str(run_id))
        await _finish_platform_run(run_id, status="failed", error=INTERNAL_ERROR)
        return
    ready = _ready(checks)
    saved = await _finish_platform_run(
        run_id, status="done", ready=ready, checks=serialize_checks(checks, PLATFORM_CHECK_LABELS)
    )
    log.info(
        "platform_readiness_finished",
        run_id=str(run_id),
        ready=ready,
        failing=[c.key for c in checks if c.ok is False],
        saved=saved,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

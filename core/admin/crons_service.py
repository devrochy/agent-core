"""Gestión de crons (ADR-0183): estado, historial, ejecución manual, pausa y reprogramación.

Único punto de lectura y escritura de `cron_job_configs` y `cron_runs` (tablas de SISTEMA, sin
RLS; `tests/test_crons_tablas_solo_desde_su_servicio.py` lo vigila). Tres usuarios:

- el wrapper de cada cron (`core/tasks/cron_runtime.py`): ¿está pausado?, lock anti-solape y
  registro de la ejecución;
- el planificador del beat (`core/tasks/cron_scheduler.py`): lee los horarios reprogramados;
- las rutas master-only de `core/api/routers/crons.py`: listar, detalle, historial y acciones.

Quien llama a las funciones de administración (las rutas) ya verificó el rol master.

Solo guarda la CLASE de la excepción de un cron fallido, nunca su mensaje: un error de SQLAlchemy
trae los parámetros de la consulta, que pueden ser datos de un negocio.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import CronJobConfig, CronRun, utcnow
from core.db.session import get_session
from core.memory.working import get_redis
from core.shared.logger import get_logger
from core.tasks.cron_catalog import (
    CATALOGO,
    MODULOS,
    SIN_CLASIFICAR,
    CronMeta,
    get_meta,
    parse_expression,
    validate_schedule,
)

log = get_logger(__name__)

LOCK_PREFIX = "cron:run:"
# Por encima del `task_time_limit=300` de Celery: un worker muerto suelta el lock solo.
LOCK_TTL_S = 330
# Una ejecución `running` de más de esto se da por perdida (worker caído o tarea matada).
STALE_RUNNING = timedelta(minutes=10)
RETENCION_DIAS = 30
INTERRUMPIDA = "Interrumpida"

ESTADOS = ("paused", "running", "failing", "ok", "never", "untracked")


class CronNotFound(Exception):
    """El cron no está en el catálogo."""


class CronConflict(Exception):
    """La acción no aplica al estado actual (ya corre, es crítico, no es reprogramable...)."""


class CronInvalid(ValueError):
    """La expresión de horario no es válida."""


class EnqueueFailed(Exception):
    """El broker no aceptó la ejecución manual."""


# --------------------------------------------------------------------------- runtime del cron
@dataclass(frozen=True)
class ConfigCron:
    paused: bool
    schedule_override: str | None


async def cargar_config() -> dict[str, ConfigCron]:
    """Estado operativo de todos los crons que tienen fila (el resto usa los valores por defecto)."""
    async with get_session() as session:
        rows = (await session.execute(select(CronJobConfig))).scalars().all()
    return {r.task_name: ConfigCron(bool(r.paused), r.schedule_override) for r in rows}


async def esta_pausado(task_name: str) -> bool:
    async with get_session() as session:
        paused = (
            await session.execute(select(CronJobConfig.paused).where(CronJobConfig.task_name == task_name))
        ).scalar_one_or_none()
    return bool(paused)


async def tomar_lock(task_name: str) -> bool:
    """Una sola ejecución a la vez por cron (programada o manual): `SET NX EX`."""
    return bool(await get_redis().set(LOCK_PREFIX + task_name, "1", nx=True, ex=LOCK_TTL_S))


async def soltar_lock(task_name: str) -> None:
    await get_redis().delete(LOCK_PREFIX + task_name)


async def lock_activo(task_name: str) -> bool:
    return bool(await get_redis().exists(LOCK_PREFIX + task_name))


async def iniciar_ejecucion(task_name: str, trigger: str, triggered_by: uuid.UUID | None) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with get_session() as session:
        session.add(
            CronRun(
                id=run_id, task_name=task_name, status="running", trigger=trigger,
                triggered_by=triggered_by, started_at=utcnow(),
            )
        )
    return run_id


async def cerrar_ejecucion(
    run_id: uuid.UUID, status: str, duration_ms: int, error_class: str | None = None
) -> None:
    async with get_session() as session:
        await session.execute(
            update(CronRun)
            .where(CronRun.id == run_id)
            .values(status=status, finished_at=utcnow(), duration_ms=duration_ms, error_class=error_class)
        )


async def registrar_ejecucion(
    task_name: str,
    status: str,
    trigger: str,
    triggered_by: uuid.UUID | None,
    started_at: datetime,
    duration_ms: int,
    error_class: str | None = None,
    detail: str | None = None,
) -> None:
    """Fila ya cerrada: un fallo de un cron que no guarda sus éxitos o una ejecución omitida."""
    async with get_session() as session:
        session.add(
            CronRun(
                task_name=task_name, status=status, trigger=trigger, triggered_by=triggered_by,
                started_at=started_at, finished_at=started_at + timedelta(milliseconds=duration_ms),
                duration_ms=duration_ms, error_class=error_class, detail=detail,
            )
        )


async def mantenimiento(ahora: datetime | None = None) -> dict[str, int]:
    """Borra el historial vencido y cierra como fallidas las ejecuciones que quedaron colgadas."""
    ahora = ahora or utcnow()
    async with get_session() as session:
        colgadas = await session.execute(
            update(CronRun)
            .where(CronRun.status == "running", CronRun.started_at < ahora - STALE_RUNNING)
            .values(status="failed", finished_at=ahora, error_class=INTERRUMPIDA)
        )
        borradas = await session.execute(
            delete(CronRun).where(CronRun.started_at < ahora - timedelta(days=RETENCION_DIAS))
        )
    return {"colgadas": colgadas.rowcount or 0, "borradas": borradas.rowcount or 0}


# --------------------------------------------------------------------------- vistas
@dataclass
class UltimaEjecucion:
    id: uuid.UUID
    status: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    error_class: str | None
    detail: str | None


@dataclass
class CronView:
    name: str
    module: str
    label: str
    description: str
    default_schedule: str | None
    interval_s: float | None
    schedule: str | None
    overridden: bool
    paused: bool
    critical: bool
    sensitive: bool
    reprogramable: bool
    record_success: bool
    estado: str
    next_run_at: datetime | None
    last_run: UltimaEjecucion | None
    last_success_at: datetime | None
    runs_24h: int
    failures_24h: int
    acciones: list[str]
    updated_at: datetime | None = None


def proxima_ejecucion(expression: str, ahora: datetime) -> datetime:
    """Próximo disparo (estrictamente posterior a `ahora`) de una expresión de 5 campos, en UTC."""
    inicio, delta, _ = parse_expression(expression).remaining_delta(ahora)
    return inicio + delta


def acciones_validas(meta: CronMeta, paused: bool, overridden: bool) -> list[str]:
    acciones = ["run"]
    if not meta.critical:
        acciones.append("resume" if paused else "pause")
    if meta.reprogramable:
        acciones.append("schedule")
        if overridden:
            acciones.append("reset_schedule")
    return acciones


def _como_ejecucion(row: CronRun, ahora: datetime) -> UltimaEjecucion:
    status, error_class = row.status, row.error_class
    if status == "running" and row.started_at < ahora - STALE_RUNNING:
        status, error_class = "failed", INTERRUMPIDA
    return UltimaEjecucion(
        id=row.id, status=status, trigger=row.trigger, started_at=row.started_at,
        finished_at=row.finished_at, duration_ms=row.duration_ms, error_class=error_class,
        detail=row.detail,
    )


def _estado(meta: CronMeta, paused: bool, last: UltimaEjecucion | None) -> str:
    if paused:
        return "paused"
    if last is None:
        return "never" if meta.record_success else "untracked"
    if last.status == "running":
        return "running"
    if last.status == "failed":
        return "failing"
    return "ok"


def _vista(
    meta: CronMeta,
    config: ConfigCron | None,
    last: UltimaEjecucion | None,
    last_success_at: datetime | None,
    runs_24h: int,
    failures_24h: int,
    ahora: datetime,
    updated_at: datetime | None,
) -> CronView:
    paused = bool(config and config.paused)
    override = config.schedule_override if config and meta.reprogramable else None
    schedule = override or meta.default
    next_run = None
    if schedule and not paused:
        try:
            next_run = proxima_ejecucion(schedule, ahora)
        except ValueError:
            log.error("cron_override_invalido", task=meta.name)
    return CronView(
        name=meta.name, module=meta.module, label=meta.label, description=meta.description,
        default_schedule=meta.default, interval_s=meta.interval_s, schedule=schedule,
        overridden=override is not None, paused=paused, critical=meta.critical,
        sensitive=meta.sensitive, reprogramable=meta.reprogramable, record_success=meta.record_success,
        estado=_estado(meta, paused, last), next_run_at=next_run, last_run=last,
        last_success_at=last_success_at, runs_24h=runs_24h, failures_24h=failures_24h,
        acciones=acciones_validas(meta, paused, override is not None), updated_at=updated_at,
    )


async def _cargar_vistas(nombres: list[str], ahora: datetime) -> dict[str, CronView]:
    desde = ahora - timedelta(hours=24)
    async with get_session() as session:
        configs = {
            r.task_name: r for r in (await session.execute(select(CronJobConfig))).scalars().all()
        }
        ultimas = {
            r.task_name: r
            for r in (
                await session.execute(
                    select(CronRun).distinct(CronRun.task_name).order_by(CronRun.task_name, CronRun.started_at.desc())
                )
            ).scalars().all()
        }
        agregados = {
            row.task_name: row
            for row in (
                await session.execute(
                    select(
                        CronRun.task_name,
                        func.max(CronRun.started_at).filter(CronRun.status == "success").label("last_success"),
                        func.count().filter(CronRun.started_at >= desde).label("runs_24h"),
                        func.count().filter(CronRun.started_at >= desde, CronRun.status == "failed").label("fails_24h"),
                    ).group_by(CronRun.task_name)
                )
            ).all()
        }
    vistas: dict[str, CronView] = {}
    for nombre in nombres:
        meta = CATALOGO[nombre]
        cfg = configs.get(nombre)
        agg = agregados.get(nombre)
        vistas[nombre] = _vista(
            meta,
            ConfigCron(bool(cfg.paused), cfg.schedule_override) if cfg else None,
            _como_ejecucion(ultimas[nombre], ahora) if nombre in ultimas else None,
            agg.last_success if agg else None,
            int(agg.runs_24h) if agg else 0,
            int(agg.fails_24h) if agg else 0,
            ahora,
            cfg.updated_at if cfg else None,
        )
    return vistas


async def listar(
    module: str | None = None, estado: str | None = None, q: str | None = None,
    ahora: datetime | None = None,
) -> list[CronView]:
    ahora = ahora or utcnow()
    vistas = await _cargar_vistas(list(CATALOGO), ahora)
    orden = {m: i for i, m in enumerate(MODULOS)}
    items = sorted(vistas.values(), key=lambda v: (orden.get(v.module, 99), v.label))
    if module:
        items = [v for v in items if v.module == module]
    if estado:
        items = [v for v in items if v.estado == estado]
    if q:
        needle = q.strip().lower()
        items = [v for v in items if needle in v.name.lower() or needle in v.label.lower()]
    return items


@dataclass
class Resumen:
    total: int
    activos: int
    pausados: int
    con_fallo: int
    ejecutando: int
    modulos: list[dict[str, Any]]


async def resumen(ahora: datetime | None = None) -> Resumen:
    items = await listar(ahora=ahora)
    modulos: list[dict[str, Any]] = []
    for nombre in [*MODULOS, SIN_CLASIFICAR]:
        del_modulo = [v for v in items if v.module == nombre]
        if del_modulo:
            modulos.append(
                {
                    "module": nombre,
                    "total": len(del_modulo),
                    "pausados": sum(1 for v in del_modulo if v.paused),
                    "con_fallo": sum(1 for v in del_modulo if v.estado == "failing"),
                }
            )
    return Resumen(
        total=len(items),
        activos=sum(1 for v in items if not v.paused),
        pausados=sum(1 for v in items if v.paused),
        con_fallo=sum(1 for v in items if v.estado == "failing"),
        ejecutando=sum(1 for v in items if v.estado == "running"),
        modulos=modulos,
    )


async def detalle(name: str, ahora: datetime | None = None) -> CronView:
    if get_meta(name) is None:
        raise CronNotFound(name)
    return (await _cargar_vistas([name], ahora or utcnow()))[name]


async def ejecuciones(
    name: str, offset: int, limit: int, status: str | None = None, trigger: str | None = None,
    ahora: datetime | None = None,
) -> tuple[list[UltimaEjecucion], int]:
    if get_meta(name) is None:
        raise CronNotFound(name)
    ahora = ahora or utcnow()
    filtros = [CronRun.task_name == name]
    if status:
        filtros.append(CronRun.status == status)
    if trigger:
        filtros.append(CronRun.trigger == trigger)
    async with get_session() as session:
        total = (await session.execute(select(func.count()).select_from(CronRun).where(*filtros))).scalar_one()
        rows = (
            await session.execute(
                select(CronRun).where(*filtros).order_by(CronRun.started_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
    return [_como_ejecucion(r, ahora) for r in rows], int(total)


# --------------------------------------------------------------------------- acciones
async def _guardar_config(
    name: str, admin_id: uuid.UUID | None, **valores: Any
) -> None:
    values = {"task_name": name, "updated_by": admin_id, "updated_at": utcnow(), **valores}
    stmt = pg_insert(CronJobConfig).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CronJobConfig.task_name],
        set_={k: v for k, v in values.items() if k != "task_name"},
    )
    async with get_session() as session:
        await session.execute(stmt)


def _meta_o_404(name: str) -> CronMeta:
    meta = get_meta(name)
    if meta is None:
        raise CronNotFound(name)
    return meta


async def pausar(name: str, admin_id: uuid.UUID | None) -> CronView:
    meta = _meta_o_404(name)
    if meta.critical:
        raise CronConflict("Este cron es crítico para la plataforma: no se puede pausar.")
    await _guardar_config(name, admin_id, paused=True)
    log.info("cron_pausado", task=name, admin=str(admin_id))
    return await detalle(name)


async def reanudar(name: str, admin_id: uuid.UUID | None) -> CronView:
    _meta_o_404(name)
    await _guardar_config(name, admin_id, paused=False)
    log.info("cron_reanudado", task=name, admin=str(admin_id))
    return await detalle(name)


async def reprogramar(name: str, expression: str, admin_id: uuid.UUID | None) -> CronView:
    meta = _meta_o_404(name)
    if not meta.reprogramable:
        raise CronConflict("Este cron corre a intervalo fijo: no se puede reprogramar.")
    try:
        normalizada = validate_schedule(expression)
    except ValueError as exc:
        raise CronInvalid(str(exc)) from exc
    # Igual al valor por defecto = quitar la reprogramación (no dejar un override que no cambia nada).
    await _guardar_config(name, admin_id, schedule_override=None if normalizada == meta.default else normalizada)
    log.info("cron_reprogramado", task=name, admin=str(admin_id), cron=normalizada)
    return await detalle(name)


async def restaurar_horario(name: str, admin_id: uuid.UUID | None) -> CronView:
    _meta_o_404(name)
    await _guardar_config(name, admin_id, schedule_override=None)
    log.info("cron_horario_restaurado", task=name, admin=str(admin_id))
    return await detalle(name)


async def ejecutar_ahora(name: str, admin_id: uuid.UUID | None) -> None:
    """Encola una ejecución manual. Ignora la pausa (es una orden explícita) pero respeta el lock:
    si ya corre, no se solapa."""
    _meta_o_404(name)
    if await lock_activo(name):
        raise CronConflict("Este cron ya se está ejecutando. Espera a que termine.")
    from core.tasks.celery_app import celery

    kwargs = {"trigger": "manual", "triggered_by": str(admin_id) if admin_id else None}
    try:
        await asyncio.to_thread(celery.send_task, name, kwargs=kwargs)
    except Exception as exc:
        log.error("cron_encolado_fallido", task=name, error=type(exc).__name__)
        raise EnqueueFailed("No se pudo encolar la ejecución. Vuelve a intentarlo.") from exc
    log.info("cron_ejecucion_manual", task=name, admin=str(admin_id))

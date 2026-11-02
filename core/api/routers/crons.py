"""Gestión de crons de la plataforma (master): `/api/v1/admin/platform/crons` (ADR-0183).

Catálogo clasificado por módulo, estado, historial de ejecuciones, ejecución manual, pausa y
reprogramación de las tareas periódicas del beat. Los crons son globales de la plataforma (no
de un negocio): solo el master, también en test. Un tester NO opera crons (cobrar, avisar a
clientes): `es_master` estricto, no `actua_como_master`.

Las rutas solo hablan con `core/admin/crons_service.py`, que es el único que toca las tablas.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from core.admin import crons_service as svc
from core.admin.roles import es_master
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params

router = APIRouter(prefix="/admin/platform/crons", tags=["admin · crons"])

CronEstado = Literal["paused", "running", "failing", "ok", "never", "untracked"]
CronAccion = Literal["run", "pause", "resume", "schedule", "reset_schedule"]
RunStatus = Literal["running", "success", "failed", "skipped"]
RunTrigger = Literal["scheduled", "manual"]


def _require_master(principal: Principal) -> None:
    if not es_master(principal.admin):
        raise ApiError.forbidden("Solo el admin de plataforma puede gestionar los crons.")


class CronRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: RunStatus
    trigger: RunTrigger
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    # Solo la clase de la excepción (nunca su mensaje).
    error_class: str | None
    # Motivo de una ejecución omitida: `plugin_inactivo`, `en_ejecucion`.
    detail: str | None


class CronOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    module: str
    label: str
    description: str
    default_schedule: str | None = Field(description="Expresión de 5 campos (UTC) del código.")
    interval_s: float | None = Field(description="Segundos, en los crons de intervalo fijo.")
    schedule: str | None = Field(description="Horario vigente: el reprogramado o el por defecto.")
    overridden: bool
    paused: bool
    critical: bool
    sensitive: bool
    reprogramable: bool
    record_success: bool = Field(description="False: no se guarda cada ejecución exitosa.")
    estado: CronEstado
    next_run_at: datetime | None
    last_run: CronRunOut | None
    last_success_at: datetime | None
    runs_24h: int
    failures_24h: int
    acciones: list[CronAccion] = Field(description="Acciones válidas para su estado actual.")
    updated_at: datetime | None


class CronList(BaseModel):
    items: list[CronOut]
    modules: list[str] = Field(description="Módulos en el orden de la pantalla.")


class CronModuleSummary(BaseModel):
    module: str
    total: int
    pausados: int
    con_fallo: int


class CronSummary(BaseModel):
    total: int
    activos: int
    pausados: int
    con_fallo: int
    ejecutando: int
    modulos: list[CronModuleSummary]


class CronScheduleIn(BaseModel):
    schedule: str = Field(
        min_length=1, max_length=100,
        description="Cinco campos: minuto hora día-del-mes mes día-de-la-semana (UTC).",
    )


class CronRunQueued(BaseModel):
    queued: bool = True
    message: str


def _vista(view: svc.CronView) -> CronOut:
    return CronOut.model_validate(view)


async def _con_errores(operacion: Awaitable[Any]) -> Any:
    try:
        return await operacion
    except svc.CronNotFound as exc:
        raise ApiError.not_found("Ese cron no existe.") from exc
    except svc.CronInvalid as exc:
        raise ApiError.bad_request(str(exc)[:180], "invalid_schedule") from exc
    except svc.CronConflict as exc:
        raise ApiError(409, "cron_conflict", str(exc)[:180]) from exc
    except svc.EnqueueFailed as exc:
        raise ApiError(502, "enqueue_failed", str(exc)[:180]) from exc


@router.get("", response_model=CronList)
async def list_crons(
    principal: Annotated[Principal, Depends(get_principal)],
    module: Annotated[str | None, Query(description="Filtra por módulo.")] = None,
    estado: Annotated[CronEstado | None, Query(description="Filtra por estado.")] = None,
    q: Annotated[str | None, Query(max_length=80, description="Busca por nombre o etiqueta.")] = None,
) -> CronList:
    _require_master(principal)
    from core.tasks.cron_catalog import MODULOS

    items = await svc.listar(module=module, estado=estado, q=q)
    return CronList(items=[_vista(v) for v in items], modules=list(MODULOS))


@router.get("/summary", response_model=CronSummary)
async def crons_summary(principal: Annotated[Principal, Depends(get_principal)]) -> CronSummary:
    _require_master(principal)
    r = await svc.resumen()
    return CronSummary(
        total=r.total, activos=r.activos, pausados=r.pausados, con_fallo=r.con_fallo,
        ejecutando=r.ejecutando, modulos=[CronModuleSummary(**m) for m in r.modulos],
    )


@router.get("/{name}", response_model=CronOut)
async def get_cron(name: str, principal: Annotated[Principal, Depends(get_principal)]) -> CronOut:
    _require_master(principal)
    return _vista(await _con_errores(svc.detalle(name)))


@router.get("/{name}/runs", response_model=Page[CronRunOut])
async def list_cron_runs(
    name: str,
    principal: Annotated[Principal, Depends(get_principal)],
    params: Annotated[PageParams, Depends(page_params)],
    status: Annotated[RunStatus | None, Query()] = None,
    trigger: Annotated[RunTrigger | None, Query()] = None,
) -> Page[CronRunOut]:
    _require_master(principal)
    rows, total = await _con_errores(
        svc.ejecuciones(name, params.offset, params.page_size, status=status, trigger=trigger)
    )
    return build_page([CronRunOut.model_validate(r) for r in rows], total, params)


@router.post("/{name}/run", response_model=CronRunQueued, status_code=202)
async def run_cron_now(name: str, principal: Annotated[Principal, Depends(get_principal)]) -> CronRunQueued:
    """Encola una ejecución manual. Ignora la pausa; no se solapa con otra en curso (409)."""
    _require_master(principal)
    await _con_errores(svc.ejecutar_ahora(name, principal.admin.id))
    return CronRunQueued(message="Ejecución encolada. Aparecerá en el historial en unos segundos.")


@router.post("/{name}/pause", response_model=CronOut)
async def pause_cron(name: str, principal: Annotated[Principal, Depends(get_principal)]) -> CronOut:
    _require_master(principal)
    return _vista(await _con_errores(svc.pausar(name, principal.admin.id)))


@router.post("/{name}/resume", response_model=CronOut)
async def resume_cron(name: str, principal: Annotated[Principal, Depends(get_principal)]) -> CronOut:
    _require_master(principal)
    return _vista(await _con_errores(svc.reanudar(name, principal.admin.id)))


@router.put("/{name}/schedule", response_model=CronOut)
async def reschedule_cron(
    name: str, payload: CronScheduleIn, principal: Annotated[Principal, Depends(get_principal)]
) -> CronOut:
    """Cambia el horario (5 campos, UTC). El beat lo aplica en ~30 s, sin reiniciar."""
    _require_master(principal)
    return _vista(await _con_errores(svc.reprogramar(name, payload.schedule, principal.admin.id)))


@router.delete("/{name}/schedule", response_model=CronOut)
async def reset_cron_schedule(
    name: str, principal: Annotated[Principal, Depends(get_principal)]
) -> CronOut:
    """Vuelve al horario por defecto del código."""
    _require_master(principal)
    return _vista(await _con_errores(svc.restaurar_horario(name, principal.admin.id)))

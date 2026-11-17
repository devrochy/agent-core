"""Automatizaciones del negocio: `/api/v1/admin/automatizaciones` (ADR-0201).

Lista los crons de ámbito `negocio` de los packs habilitados, con su política (encendido/apagado,
hora local y ventana), la última ejecución y la próxima. El dueño y el master escriben; un
colaborador necesita `write` en la sección `automatizaciones` (el dueño se lo otorga en
Colaboradores). Los crons de plataforma siguen en `Plataforma › Crons` (master, ADR-0183).

Los parámetros de dominio (horas de reenganche, recordatorios…) no se duplican aquí: la pantalla
enlaza a la configuración del pack. Un cron sensible pide confirmación en el panel; la API deja
la auditoría en `audit_log`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.admin import crons_negocio_service as svc
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params

router = APIRouter(prefix="/admin/automatizaciones", tags=["admin · automatizaciones"])

EstadoEjecucion = Literal["success", "failed", "skipped"]
AccionAutomatizacion = Literal["enable", "disable", "hora", "ventana"]


class Ventana(BaseModel):
    # La validación fina (HH:MM y días 0–6) la hace el servicio para devolver un 400 con
    # `invalid_schedule`, no un 422 de Pydantic.
    desde: str = Field(description="HH:MM en hora local.")
    hasta: str = Field(description="HH:MM en hora local.")
    dias: list[int] = Field(
        default_factory=lambda: list(range(7)),
        description="0=lunes … 6=domingo. Vacío = todos los días.",
    )


class CronNegocioOut(BaseModel):
    name: str
    module: str
    label: str
    description: str
    pack: str | None = Field(description="Pack del que cuelga; enlaza a su configuración.")
    sensitive: bool
    configurable: bool
    admite_hora_local: bool
    admite_ventana: bool
    parametros: list[str] = Field(description="Campos del pack que la pantalla enlaza.")
    habilitada: bool
    hora_local: int | None = Field(description="Hora local efectiva (la elegida o la del catálogo).")
    hora_local_por_defecto: int | None = Field(description="Hora local que se usa si el negocio no elige.")
    hora_local_personalizada: bool = Field(description="True si el negocio eligió una hora propia.")
    ventana: Ventana | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: EstadoEjecucion | None
    last_resumen: str | None
    last_acciones: int | None
    acciones: list[AccionAutomatizacion] = Field(description="Acciones válidas para esta automatización.")


class AutomatizacionesResumen(BaseModel):
    total: int
    habilitadas: int
    apagadas: int
    con_fallo: int


class AutomatizacionesOut(BaseModel):
    items: list[CronNegocioOut]
    resumen: AutomatizacionesResumen


class CronNegocioRunOut(BaseModel):
    id: uuid.UUID
    status: EstadoEjecucion
    trigger: Literal["scheduled", "manual"]
    acciones: int
    resumen: str | None
    error_class: str | None
    detail: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


class CronNegocioUpdate(BaseModel):
    habilitada: bool | None = None
    hora_local: int | None = Field(default=None, ge=0, le=23)
    ventana: Ventana | None = None


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Elige un negocio para ver sus automatizaciones.", "tenant_required")
    return principal.tenant_id


def _vista(v: svc.CronNegocioView) -> CronNegocioOut:
    return CronNegocioOut(
        name=v.name,
        module=v.module,
        label=v.label,
        description=v.description,
        pack=v.pack,
        sensitive=v.sensitive,
        configurable=v.configurable,
        admite_hora_local=v.admite_hora_local,
        admite_ventana=v.admite_ventana,
        parametros=list(v.parametros),
        habilitada=v.habilitada,
        hora_local=v.hora_local,
        hora_local_por_defecto=v.hora_local_por_defecto,
        hora_local_personalizada=v.hora_local_personalizada,
        ventana=Ventana(**v.ventana) if v.ventana else None,
        next_run_at=v.next_run_at,
        last_run_at=v.last_run_at,
        last_status=v.last_status,  # type: ignore[arg-type]
        last_resumen=v.last_resumen,
        last_acciones=v.last_acciones,
        acciones=v.acciones,  # type: ignore[arg-type]
    )


@router.get("", response_model=AutomatizacionesOut)
async def listar_automatizaciones(
    principal: Annotated[Principal, Depends(require("automatizaciones"))],
) -> AutomatizacionesOut:
    tenant_id = _tenant(principal)
    items = [_vista(v) for v in await svc.listar(tenant_id)]
    return AutomatizacionesOut(
        items=items,
        resumen=AutomatizacionesResumen(
            total=len(items),
            habilitadas=sum(1 for i in items if i.habilitada),
            apagadas=sum(1 for i in items if not i.habilitada),
            con_fallo=sum(1 for i in items if i.last_status == "failed"),
        ),
    )


@router.get("/{cron_name}", response_model=CronNegocioOut)
async def detalle_automatizacion(
    cron_name: str,
    principal: Annotated[Principal, Depends(require("automatizaciones"))],
) -> CronNegocioOut:
    tenant_id = _tenant(principal)
    for vista in await svc.listar(tenant_id):
        if vista.name == cron_name:
            return _vista(vista)
    raise ApiError.not_found("Esa automatización no existe o no es de este negocio.")


@router.put("/{cron_name}", response_model=CronNegocioOut)
async def actualizar_automatizacion(
    cron_name: str,
    payload: CronNegocioUpdate,
    principal: Annotated[Principal, Depends(require("automatizaciones", write=True))],
) -> CronNegocioOut:
    """Enciende/apaga o cambia hora/ventana. Solo lo que el catálogo admite."""
    tenant_id = _tenant(principal)
    enviados = payload.model_dump(exclude_unset=True)
    kwargs: dict[str, Any] = {}
    if "habilitada" in enviados:
        if payload.habilitada is None:
            raise ApiError.bad_request("Indica si la automatización queda encendida o apagada.", "invalid_enabled")
        kwargs["habilitada"] = payload.habilitada
    if "hora_local" in enviados:
        kwargs["hora_local"] = payload.hora_local
    if "ventana" in enviados:
        kwargs["ventana"] = payload.ventana.model_dump() if payload.ventana else None
    try:
        await svc.fijar(tenant_id, cron_name, updated_by=principal.admin.id, **kwargs)
    except svc.CronNegocioNoEncontrado as exc:
        raise ApiError.not_found("Esa automatización no existe o no es de este negocio.") from exc
    except svc.CronNegocioNoConfigurable as exc:
        raise ApiError.bad_request(str(exc)[:180], "not_configurable") from exc
    except svc.CronNegocioInvalido as exc:
        raise ApiError.bad_request(str(exc)[:180], "invalid_schedule") from exc
    for vista in await svc.listar(tenant_id):
        if vista.name == cron_name:
            return _vista(vista)
    raise ApiError.not_found("Esa automatización no existe o no es de este negocio.")


@router.get("/{cron_name}/runs", response_model=Page[CronNegocioRunOut])
async def listar_ejecuciones(
    cron_name: str,
    principal: Annotated[Principal, Depends(require("automatizaciones"))],
    params: Annotated[PageParams, Depends(page_params)],
    status: Annotated[EstadoEjecucion | None, Query()] = None,
) -> Page[CronNegocioRunOut]:
    """Ejecuciones con efecto, fallos y manuales del negocio (ADR-0201)."""
    tenant_id = _tenant(principal)
    try:
        filas, total = await svc.historial(
            tenant_id, cron_name, offset=params.offset, limit=params.page_size, status=status
        )
    except svc.CronNegocioNoEncontrado as exc:
        raise ApiError.not_found("Esa automatización no existe o no es de este negocio.") from exc
    return build_page(
        [
            CronNegocioRunOut(
                id=f.id,
                status=f.status,  # type: ignore[arg-type]
                trigger=f.trigger,  # type: ignore[arg-type]
                acciones=f.acciones,
                resumen=f.resumen,
                error_class=f.error_class,
                detail=f.detail,
                started_at=f.started_at,
                finished_at=f.finished_at,
                duration_ms=f.duration_ms,
            )
            for f in filas
        ],
        total,
        params,
    )

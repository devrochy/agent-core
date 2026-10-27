"""Resumen por agente: `GET /api/v1/admin/dashboard/agents` (ADR-0171).

KPIs del negocio (el Coordinador) y, por cada agente, su actividad y la de los módulos que gestiona
(pedidos, citas, recaudo, suscripciones…), cada uno con el valor de los últimos 7 días, el de los 7
previos y un destino para ver el detalle. Lo construye `core.metricas.resumen_agentes`; los KPIs de
cada módulo los aporta su pack (`core.admin.resumen_registry`).

Permisos: la sección `dashboard` abre el endpoint; cada KPI exige además la sección que lo respalda
y, si falta, **se omite** (no sale en cero): conversaciones (`conversations`), escalaciones
(`escalations`), costo (`costs`), agentes y fiabilidad (`agents`), y cada módulo su propia sección
(`pack_cobranza:resumen`, …). Un colaborador sin ninguna recibe la lista vacía, no un 403.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.admin.permissions import effective_access
from core.admin.resumen_registry import resumen_registry
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.metricas import resumen_agentes as svc

router = APIRouter(prefix="/admin/dashboard", tags=["admin · dashboard"])


class DestinoOut(BaseModel):
    """A dónde lleva un KPI: una ruta lógica y sus filtros; cada cliente la traduce a su navegación."""

    ruta: str = Field(description="Ruta lógica: `conversaciones`, `agente`, `comercio.pedidos`, …")
    params: dict[str, str] = Field(default_factory=dict)


class KpiOut(BaseModel):
    id: str
    label: str
    valor: float | None = Field(description="`null` si no hay datos suficientes.")
    unidad: Literal["count", "pct", "usd", "money", "ms"]
    previo: float | None = Field(
        description="Mismo KPI en el período anterior; `null` si es una foto de hoy."
    )
    delta_pct: float | None = Field(
        description="Variación contra el período anterior; `null` si no se puede calcular."
    )
    polaridad: Literal["up-good", "up-bad"]
    hint: str | None = None
    destino: DestinoOut | None = None


class ModuloOut(BaseModel):
    id: str
    nombre: str
    kpis: list[KpiOut]
    error: str | None = Field(
        default=None, description="El módulo no pudo calcularse; el resto del Resumen sí."
    )


class AgenteResumen(BaseModel):
    ref: str = Field(
        description="Id del agente o el literal `general`, como en `/admin/agents/{ref}`."
    )
    key: str
    name: str
    kind: str
    kpis: list[KpiOut]
    modulos: list[ModuloOut]


class BloqueGeneral(BaseModel):
    kpis: list[KpiOut]


class PeriodoOut(BaseModel):
    desde: datetime
    hasta: datetime
    previo_desde: datetime
    previo_hasta: datetime


class ResumenPorAgente(BaseModel):
    periodo: PeriodoOut
    runtime: str
    general: BloqueGeneral
    agentes: list[AgenteResumen]


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.not_found("No hay un negocio activo.")
    return principal.tenant_id


async def _puede(principal: Principal, seccion: str) -> bool:
    return await effective_access(principal.admin, seccion, principal.tenant_id) is not None


async def _acceso(principal: Principal) -> svc.Acceso:
    modulos = frozenset(
        {
            p.seccion
            for p in resumen_registry.providers_for(None)
            if await _puede(principal, p.seccion)
        }
    )
    return svc.Acceso(
        conversaciones=await _puede(principal, "conversations"),
        escalaciones=await _puede(principal, "escalations"),
        costos=await _puede(principal, "costs"),
        agentes=await _puede(principal, "agents"),
        modulos=modulos,
    )


@router.get("/agents", response_model=ResumenPorAgente)
async def dashboard_agents(
    principal: Annotated[Principal, Depends(require("dashboard"))],
) -> ResumenPorAgente:
    datos = await svc.resumen(_tenant(principal), await _acceso(principal))
    return ResumenPorAgente.model_validate(datos)

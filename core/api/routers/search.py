"""Búsqueda global de la app oficial (`GET /api/v1/search`, ADR-0096).

Agrega los providers registrados (core + packs) respetando el RBAC por sección
del principal y el aislamiento por negocio (RLS del scope del request). Un
provider que falla se omite con log: la búsqueda nunca tumba por una sección.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.admin.permissions import effective_access
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.search import register_search, search_providers
from core.search.providers import buscar_conversaciones, buscar_leads
from core.shared.logger import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["búsqueda"])

# Providers del core: se registran una sola vez al importar el router.
register_search("conversations", "Conversaciones", buscar_conversaciones)
register_search("leads", "Clientes", buscar_leads)


class ItemBusqueda(BaseModel):
    id: str
    tipo: str
    titulo: str
    subtitulo: str
    extra: dict[str, Any] = Field(default_factory=dict)


class ResultadoSeccion(BaseModel):
    seccion: str
    label: str
    items: list[ItemBusqueda]


class SearchResponse(BaseModel):
    resultados: list[ResultadoSeccion]


@router.get("/search", response_model=SearchResponse)
async def busqueda_global(
    q: Annotated[str, Query(min_length=2, max_length=100)],
    principal: Annotated[Principal, Depends(get_principal)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> SearchResponse:
    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")

    admin = principal.admin
    tenant_id = principal.tenant_id

    async def _una(seccion: str, entrada: dict) -> ResultadoSeccion | None:
        if admin.role != "master":
            acceso = await effective_access(admin, seccion, tenant_id)
            if acceso is None:
                return None
        try:
            items = await entrada["provider"](tenant_id, q, limit)
        except Exception:
            log.warning("search_provider_error", seccion=seccion, exc_info=True)
            return None
        return ResultadoSeccion(
            seccion=seccion,
            label=entrada["label"],
            items=items,
        )

    secciones = await asyncio.gather(
        *(_una(seccion, entrada) for seccion, entrada in search_providers().items())
    )
    return SearchResponse(resultados=[s for s in secciones if s is not None])

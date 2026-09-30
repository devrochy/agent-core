"""Tenants del admin (selector de negocio): `GET /api/v1/admin/tenants`.

Lista **buscable y paginada** de los tenants a los que el admin tiene acceso, con
el activo marcado. No exige permiso de sección: es identidad (cada admin ve solo
los suyos, resueltos por `admin_tenants`, sin RLS).

`MeResponse` ya no devuelve la lista completa (un master puede tener cientos): el
panel la pide aquí a demanda.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from core.api.deps import Principal, get_principal
from core.api.pagination import Page, PageParams, build_page, page_params
from core.api.schemas import TenantOption
from core.shared.tenant import admin_tenants

router = APIRouter(prefix="/admin/tenants", tags=["admin · tenants"])


@router.get("", response_model=Page[TenantOption])
async def list_tenants(
    principal: Annotated[Principal, Depends(get_principal)],
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(description="Busca por nombre del negocio.")] = None,
) -> Page[TenantOption]:
    rows = await admin_tenants(principal.admin.id)
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in r[1].lower()]
    rows = sorted(rows, key=lambda r: r[1].lower())
    total = len(rows)
    window = rows[params.offset : params.offset + params.page_size]

    items = [
        TenantOption(
            id=tid,
            name=name,
            role=role,
            active=tid == principal.tenant_id,
        )
        for (tid, name, role) in window
    ]
    return build_page(items, total, params)

"""Sync incremental de la app oficial (`GET /api/v1/sync/{entidad}`, ADR-0097).

Ejecuta el adapter registrado para la entidad, previa validación de RBAC por
sección (`effective_access`, master bypass) dentro del scope de tenant del
request. Contrato: `{entidad, items, nextCursor, deletedIds}`.
"""

from __future__ import annotations

from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.admin.permissions import effective_access
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.shared.logger import get_logger
from core.sync import ejecutar_sync, parse_cursor, sync_adapters
from core.sync import providers as _providers  # noqa: F401  (registra los del core)

log = get_logger(__name__)

router = APIRouter(tags=["sync"])


class SyncResponse(BaseModel):
    entidad: str
    items: list[dict]
    nextCursor: str
    deletedIds: list[str]


@router.get("/sync/{entidad}", response_model=SyncResponse)
async def sync_entidad(
    entidad: str,
    principal: Annotated[Principal, Depends(get_principal)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> SyncResponse:
    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")

    adapter = sync_adapters().get(entidad)
    if adapter is None:
        raise ApiError(404, "entidad_desconocida", f"Entidad de sync desconocida: {entidad}")

    if principal.admin.role != "master":
        acceso = await effective_access(principal.admin, adapter.seccion, principal.tenant_id)
        if acceso is None:
            raise ApiError.forbidden(f"Sin permiso para la sección '{adapter.seccion}'.")

    try:
        cursor_dt = parse_cursor(cursor)
    except ValueError:
        raise ApiError(400, "cursor_invalido", "El cursor debe ser una fecha ISO-8601.") from None

    try:
        items, next_dt, deleted_ids = await ejecutar_sync(
            adapter, principal.tenant_id, cursor_dt, limit
        )
    except Exception:
        log.warning("sync_error", entidad=entidad, exc_info=True)
        raise ApiError(500, "sync_error", "No se pudo sincronizar esta entidad.") from None

    return SyncResponse(
        entidad=entidad,
        items=items,
        nextCursor=next_dt.astimezone(UTC).isoformat(),
        deletedIds=deleted_ids,
    )

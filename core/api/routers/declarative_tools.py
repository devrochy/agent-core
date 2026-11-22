"""Editor de tools declarativas del negocio en `/api/v1`: retirado (ADR-0206, HU-AOS-91).

Editaba `extensions.tools` del AgentSpec v2, que el turno ya no lee desde HU-AOS-90: las declarativas que corren son
las de cada agente (`AgentSpecV3.declarativeTools`). `GET` y `PUT /api/v1/admin/settings/declarative-tools`
responden 410 `v2_retirado`; editarlas por agente queda para una HU nueva (decisión del dueño del 2026-11-22).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from core.api.deps import Principal, require
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/settings/declarative-tools", tags=["admin · configuración"])

_RETIRADO: dict[int | str, dict[str, Any]] = {410: {"description": "Retirado con el AgentSpec v2: `error.code = v2_retirado` (ADR-0206, HU-AOS-91)."}}


def _retirado() -> ApiError:
    return ApiError(
        410,
        "v2_retirado",
        "Las tools declarativas del negocio eran del AgentSpec v2, que el turno ya no lee (ADR-0206, HU-AOS-91).",
    )


@router.get("", status_code=410, responses=_RETIRADO)
async def get_declarative_tools(principal: Annotated[Principal, Depends(require("settings"))]) -> None:
    raise _retirado()


@router.put("", status_code=410, responses=_RETIRADO)
async def save_declarative_tools(principal: Annotated[Principal, Depends(require("settings", write=True))]) -> None:
    raise _retirado()

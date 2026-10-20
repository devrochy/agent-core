"""Datos de prueba por agente (HU-QA-10, ADR-0146). Solo en test.

- `GET /api/v1/admin/agents/{agent_id}/datos-de-prueba` — si el agente tiene datos que generar,
  si falta la descripción del negocio y el último lote (origen y conteos por tipo).
- `POST …` — genera según la descripción del negocio (LLM con plantilla de respaldo), reemplaza
  el lote anterior del agente y siembra el nuevo. 409 `en_curso` con otra generación del mismo
  agente; 409 `sin_datos` si el agente no tiene forma; 422 `descripcion_requerida`.
- `DELETE …` — retira los datos de prueba del agente (borrado lógico en el pack).

Sin `QA_TESTERS_ENABLED` las tres responden 404: en producción la función no existe. Permiso:
sección `agents` (escritura para generar o borrar). El agente se busca con el `tenant_id` de la
sesión además de la RLS: uno de otro negocio es 404 sin eco.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.config import get_settings

router = APIRouter(prefix="/admin/agents", tags=["admin · agentes"])



class FormaDeDatosOut(BaseModel):
    modo: str
    titulo: str


class LoteDePrueba(BaseModel):
    lote_id: uuid.UUID
    modo: str
    origen: Literal["llm", "plantilla"]
    creado_en: datetime
    conteos: dict[str, int] = Field(description="Registros creados por tipo (`producto`, `cita`, `cliente`…).")


class EstadoDatosDePrueba(BaseModel):
    disponible: bool
    motivo: str | None = Field(default=None, description="Por qué el agente no tiene datos que generar.")
    falta_descripcion: bool = Field(description="El negocio no tiene descripción propia: hay que escribirla antes.")
    forma: FormaDeDatosOut | None
    lote: LoteDePrueba | None


class LoteGenerado(LoteDePrueba):
    motivo_plantilla: Literal["proveedor", "formato"] | None = Field(
        default=None, description="Por qué se usó la plantilla: el LLM falló o su respuesta no cumplió el formato."
    )
    retirados: int = Field(description="Registros del lote anterior que se retiraron.")


class DatosRetirados(BaseModel):
    retirados: int


def _habilitado() -> None:
    if not get_settings().qa_testers_enabled:
        raise ApiError.not_found()


def _error(exc: Exception) -> ApiError:
    from core.qa.datos_generados import ErrorDatosDePrueba

    assert isinstance(exc, ErrorDatosDePrueba)
    return ApiError(exc.status, exc.code, exc.message)


@router.get("/{agent_id}/datos-de-prueba", response_model=EstadoDatosDePrueba)
async def estado_datos_de_prueba(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents"))]
) -> dict[str, object]:
    """Estado de los datos de prueba del agente."""
    from core.qa import datos_generados as svc

    _habilitado()
    try:
        return await svc.estado(principal.tenant_id, agent_id)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.post("/{agent_id}/datos-de-prueba", response_model=LoteGenerado, status_code=201)
async def generar_datos_de_prueba(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents", write=True))]
) -> dict[str, object]:
    """Genera los datos de prueba del agente según la descripción del negocio y reemplaza los anteriores."""
    from core.qa import datos_generados as svc

    _habilitado()
    try:
        return await svc.generar(principal.tenant_id, agent_id)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.delete("/{agent_id}/datos-de-prueba", response_model=DatosRetirados)
async def borrar_datos_de_prueba(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents", write=True))]
) -> dict[str, object]:
    """Retira los datos de prueba del agente; lo creado a mano no se toca."""
    from core.qa import datos_generados as svc

    _habilitado()
    try:
        return await svc.borrar(principal.tenant_id, agent_id)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc

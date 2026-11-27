"""Escenarios de las pruebas del agente (HU-AOS-101, SPEC-53, ADR-0212).

Bajo `/api/v1/admin/agents/{agent_id}/pruebas`, con el permiso de la sección `agents` (escribir exige
escritura; un colaborador sin ese permiso recibe 403):

- `GET` — los escenarios vivos del agente, el tope y un resumen de la última corrida.
- `POST` — el dueño escribe un escenario (`origen='manual'`); nace `borrador`, o `aprobado` si lo manda así.
- `POST /proponer` — la IA propone hasta 10 en `borrador` (`origen='ia'`) y cobra una fila `generacion`.
- `PATCH /{case_id}` — edita o aprueba (`estado`). `DELETE /{case_id}` — borrado lógico.

Errores: 404 (agente o escenario de otro negocio, sin eco), 409 `titulo_repetido`, `en_curso` y
`plan_limit_creditos` (sin saldo para proponer), 422 `tope_de_escenarios`, `tool_desconocida`,
`tools_en_conflicto`, `escenario_incompleto`; 502 `propuesta_invalida` y 503 `ia_no_disponible` al proponer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_responses

router = APIRouter(prefix="/admin/agents/{agent_id}/pruebas", tags=["admin · pruebas del agente"])

Titulo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Objetivo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Criterio = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
NombreDeTool = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Estado = Literal["borrador", "aprobado"]


class EscenarioDePruebaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    titulo: Titulo
    objetivo_cliente: Objetivo = Field(description="Lo que el cliente simulado quiere lograr.")
    datos_cliente: dict[str, Any] = Field(
        default_factory=dict, description="Datos inventados que el cliente conoce."
    )
    criterios: list[Criterio] = Field(default_factory=list, max_length=20)
    tools_esperadas: list[NombreDeTool] = Field(default_factory=list, max_length=50)
    tools_prohibidas: list[NombreDeTool] = Field(default_factory=list, max_length=50)
    estado: Estado = "borrador"


class EscenarioDePruebaPatch(BaseModel):
    """Solo los campos enviados cambian."""

    model_config = ConfigDict(extra="forbid")

    titulo: Titulo | None = None
    objetivo_cliente: Objetivo | None = None
    datos_cliente: dict[str, Any] | None = None
    criterios: list[Criterio] | None = Field(default=None, max_length=20)
    tools_esperadas: list[NombreDeTool] | None = Field(default=None, max_length=50)
    tools_prohibidas: list[NombreDeTool] | None = Field(default=None, max_length=50)
    estado: Estado | None = None


class EscenarioDePrueba(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    titulo: str
    objetivo_cliente: str
    datos_cliente: dict[str, Any]
    criterios: list[str]
    tools_esperadas: list[str]
    tools_prohibidas: list[str]
    origen: Literal["ia", "manual"]
    estado: Estado
    created_at: datetime
    updated_at: datetime


class ResumenDeCorridaDePrueba(BaseModel):
    id: uuid.UUID
    estado: str
    k: int
    iniciada_en: datetime | None
    terminada_en: datetime | None


class EscenariosDePrueba(BaseModel):
    escenarios: list[EscenarioDePrueba]
    tope: int = Field(description="Máximo de escenarios por agente.")
    ultima_corrida: ResumenDeCorridaDePrueba | None


class EscenariosPropuestos(BaseModel):
    escenarios: list[EscenarioDePrueba] = Field(
        description="Los guardados en `borrador`, hasta 10."
    )
    descartados: int = Field(
        description="Propuestas que se descartaron: repetidas, sin título u objetivo."
    )
    creditos: float | None = Field(
        description="Lo que costó pedir la propuesta; `null` si no se pudo medir."
    )


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.not_found()
    return principal.tenant_id


def _error(exc: Exception) -> ApiError:
    from core.pruebas.escenarios import ErrorPruebas

    assert isinstance(exc, ErrorPruebas)
    return ApiError(exc.status, exc.code, exc.message)


@router.get("", response_model=EscenariosDePrueba)
async def listar_escenarios(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents"))]
) -> dict[str, Any]:
    """Los escenarios del agente y su última corrida."""
    from core.pruebas import escenarios as svc

    try:
        return await svc.listar(_tenant(principal), agent_id)
    except svc.ErrorPruebas as exc:
        raise _error(exc) from exc


@router.post("", response_model=EscenarioDePrueba, status_code=201)
async def escribir_escenario(
    agent_id: uuid.UUID,
    payload: EscenarioDePruebaIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> dict[str, Any]:
    """El dueño escribe un escenario. Sus tools deben ser del agente."""
    from core.pruebas import escenarios as svc

    try:
        return await svc.crear(
            _tenant(principal),
            agent_id,
            payload.model_dump(),
            origen="manual",
            created_by=principal.admin.id,
        )
    except svc.ErrorPruebas as exc:
        raise _error(exc) from exc


@router.post(
    "/proponer",
    response_model=EscenariosPropuestos,
    status_code=201,
    responses={
        **plan_limit_responses("creditos"),
        502: {
            "description": "`propuesta_invalida`: la IA respondió sin escenarios utilizables (ya se cobró la llamada)."
        },
        503: {"description": "`ia_no_disponible`: el proveedor no respondió; no se cobró nada."},
    },
)
async def proponer_escenarios(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents", write=True))]
) -> dict[str, Any]:
    """La IA propone hasta 10 escenarios con la configuración del agente. Quedan en `borrador`."""
    from core.api.plan_limits import plan_limit_error
    from core.billing.enforcement import QuotaExceeded
    from core.pruebas import generador
    from core.pruebas.escenarios import ErrorPruebas

    try:
        return await generador.proponer(_tenant(principal), agent_id, created_by=principal.admin.id)
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from exc
    except ErrorPruebas as exc:
        raise _error(exc) from exc


@router.patch("/{case_id}", response_model=EscenarioDePrueba)
async def editar_escenario(
    agent_id: uuid.UUID,
    case_id: uuid.UUID,
    payload: EscenarioDePruebaPatch,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> dict[str, Any]:
    """Edita un escenario o lo aprueba (`estado = aprobado`)."""
    from core.pruebas import escenarios as svc

    cambios = payload.model_dump(exclude_unset=True)
    for requerido in ("titulo", "objetivo_cliente"):  # no se puede dejar en blanco con `null`
        if requerido in cambios and cambios[requerido] is None:
            raise ApiError(422, "validation_error", f"`{requerido}` no puede ser null.")
    cambios = {k: v for k, v in cambios.items() if v is not None}
    try:
        return await svc.actualizar(_tenant(principal), agent_id, case_id, cambios)
    except svc.ErrorPruebas as exc:
        raise _error(exc) from exc


@router.delete("/{case_id}", status_code=204)
async def borrar_escenario(
    agent_id: uuid.UUID,
    case_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> None:
    """Borrado lógico: el escenario deja de salir y de correrse."""
    from core.pruebas import escenarios as svc

    try:
        await svc.borrar(_tenant(principal), agent_id, case_id)
    except svc.ErrorPruebas as exc:
        raise _error(exc) from exc

"""Estimado y corridas de las pruebas del agente (SPEC-53, ADR-0212 §4 y §7, HU-AOS-102).

Bajo `/api/v1/admin/agents/{agent_id}/pruebas`, con el permiso de escritura de la sección `agents`:

- `POST …/estimado` `{case_ids}` → `por_k`: para cada `k` de 1 al tope del negocio, `estimado`, `de_la_cuota`
  (la cuota gratis compartida con el playground), `del_saldo`, `saldo_despues` y si `alcanza`. No escribe.
- `POST …/corridas` `{case_ids, k, confirmo_creditos}` → 202 con la corrida `en_cola`. `confirmo_creditos` tiene
  que ser el estimado vigente de ese `k`. Errores: 404 `escenario_no_encontrado`; 422 `k_fuera_de_rango`,
  `demasiados_escenarios`, `sin_escenarios`, `escenario_no_aprobado`; 409 `estimado_cambio`, `corrida_en_curso`,
  `plan_limit_creditos`; 429 `corridas_por_dia`. No ejecuta nada: el corredor es HU-AOS-103.

Y, solo master, el tope de `k` de un negocio: `GET|PUT /api/v1/admin/platform/tenants/{tenant_id}/pruebas`.

El costo en USD (`costo_usd`) solo sale para el master; el modelo del juez y del simulador nunca salen. Sin
`PRUEBAS_AGENTE_ENABLED` las rutas del agente responden 404. Un agente o escenario de otro negocio es 404 sin eco.
(Los escenarios y su CRUD son de HU-AOS-101, otro router.)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from core.admin.roles import actua_como_master
from core.api.deps import Principal, get_principal, require
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_error, plan_limit_responses
from core.billing.enforcement import QuotaExceeded
from core.billing.pruebas import MAX_ESCENARIOS, EstimadoDeCorrida
from core.config import get_settings
from core.pruebas import corridas as svc
from core.pruebas.config import K_TECHO

router = APIRouter(prefix="/admin/agents", tags=["admin · pruebas del agente"])
plataforma_router = APIRouter(prefix="/admin/platform", tags=["admin · plataforma"])


class PruebasEstimadoDeKOut(BaseModel):
    k: int = Field(description="Intentos por escenario.")
    estimado: int = Field(description="Créditos que cuesta correr con este `k` (es el de k = 1 por `k`).")
    de_la_cuota: int = Field(description="Cuánto sale de la cuota gratis de pruebas y playground del ciclo.")
    del_saldo: int = Field(description="Cuánto sale del saldo: `estimado menos de_la_cuota`.")
    saldo_despues: int | None = Field(description="Saldo tras la corrida; `null` si el negocio no tiene tope.")
    alcanza: bool = Field(description="Si `del_saldo` cabe en el saldo.")
    costo_usd: float | None = Field(default=None, description="Costo real en USD. Solo lo ve el master.")


class PruebasEstimadoOut(BaseModel):
    escenarios: int
    nivel_ia: str
    k_por_defecto: int
    k_maximo: int = Field(description="Tope de `k` del negocio (el de plataforma o el que fijó el master).")
    saldo: int | None = Field(description="Créditos que le quedan al negocio en el ciclo; `null` si no hay tope.")
    cuota_gratis_restante: int
    por_k: list[PruebasEstimadoDeKOut]


class PruebasEstimadoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=200,
        description=f"Escenarios aprobados del agente. Máximo {MAX_ESCENARIOS} distintos por corrida.",
    )


class PruebasCorridaIn(PruebasEstimadoIn):
    k: int = Field(description="Intentos por escenario: de 1 al tope del negocio (`k_fuera_de_rango` si no).")
    confirmo_creditos: int = Field(
        ge=0, description="Créditos que el dueño aprobó: el `estimado` de este `k`. Si cambió, 409 `estimado_cambio`."
    )


class PruebasCorridaOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    estado: str
    k: int
    escenarios: int
    creditos_estimados: float
    creditos_de_cuota: float
    tope_creditos: float
    created_at: datetime


_ERRORES: dict[int | str, dict[str, str]] = {
    404: {"description": "Agente o escenario inexistente o de otro negocio (`not_found`, `escenario_no_encontrado`), o pruebas apagadas."},
    422: {
        "description": "`k_fuera_de_rango`, `demasiados_escenarios`, `sin_escenarios` o `escenario_no_aprobado`."
    },
}


def _habilitado() -> None:
    if not get_settings().pruebas_agente_enabled:
        raise ApiError.not_found()


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.not_found()
    return principal.tenant_id


def _error(exc: svc.ErrorPruebas) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, details=exc.details)


def _estimado_out(estimado: EstimadoDeCorrida, *, master: bool) -> PruebasEstimadoOut:
    return PruebasEstimadoOut(
        escenarios=estimado.escenarios,
        nivel_ia=estimado.nivel_ia,
        k_por_defecto=estimado.k_por_defecto,
        k_maximo=estimado.k_maximo,
        saldo=estimado.saldo,
        cuota_gratis_restante=estimado.cuota_gratis_restante,
        por_k=[
            PruebasEstimadoDeKOut(
                k=e.k,
                estimado=e.estimado,
                de_la_cuota=e.de_la_cuota,
                del_saldo=e.del_saldo,
                saldo_despues=e.saldo_despues,
                alcanza=e.alcanza,
                costo_usd=float(e.costo_usd) if master else None,
            )
            for e in estimado.por_k
        ],
    )


@router.post("/{agent_id}/pruebas/estimado", response_model=PruebasEstimadoOut, response_model_exclude_none=False, responses=_ERRORES)
async def estimar(
    agent_id: uuid.UUID,
    payload: PruebasEstimadoIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> PruebasEstimadoOut:
    """Cuánto cuesta correr los escenarios con cada `k` y cuánto sale de la cuota gratis. No escribe nada."""
    _habilitado()
    try:
        estimado = await svc.estimar(_tenant(principal), agent_id, payload.case_ids)
    except svc.ErrorPruebas as exc:
        raise _error(exc) from exc
    return _estimado_out(estimado, master=actua_como_master(principal.admin))


@router.post(
    "/{agent_id}/pruebas/corridas",
    response_model=PruebasCorridaOut,
    status_code=202,
    responses={**_ERRORES, **plan_limit_responses("creditos"), 429: {"description": "`corridas_por_dia`."}},
)
async def crear_corrida(
    agent_id: uuid.UUID,
    payload: PruebasCorridaIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> PruebasCorridaOut:
    """Aprueba el estimado y deja la corrida en cola. Una a la vez por negocio, 5 por día."""
    _habilitado()
    try:
        corrida = await svc.crear(
            _tenant(principal),
            agent_id,
            payload.case_ids,
            k=payload.k,
            confirmo_creditos=payload.confirmo_creditos,
            pedida_por=principal.admin.id,
            actor=f"admin:{principal.admin.email}",
        )
    except svc.ErrorPruebas as exc:
        raise _error(exc) from exc
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from exc
    return PruebasCorridaOut(
        id=corrida.id,
        agent_id=corrida.agent_id,
        estado=corrida.estado,
        k=corrida.k,
        escenarios=len(corrida.version_probada.get("escenarios", [])),
        creditos_estimados=float(corrida.creditos_estimados),
        creditos_de_cuota=float(corrida.creditos_de_cuota),
        tope_creditos=float(corrida.tope_creditos),
        created_at=corrida.created_at,
    )


# ------------------------------------------------------------------ tope de k por negocio (solo master)
class PruebasTopeDeKOut(BaseModel):
    k_maximo: int = Field(description="Tope efectivo de `k` del negocio.")
    k_maximo_propio: int | None = Field(description="Lo que fijó el master; `null` si usa el de plataforma.")
    k_maximo_de_plataforma: int


class PruebasTopeDeKIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k_maximo: int | None = Field(
        ge=1, le=K_TECHO, description=f"Tope de `k` del negocio, de 1 a {K_TECHO}; `null` vuelve al de plataforma."
    )


def _solo_master(principal: Principal) -> None:
    if not actua_como_master(principal.admin):
        raise ApiError.forbidden("Solo el admin de plataforma puede cambiar el tope de las pruebas.")


@plataforma_router.get("/tenants/{tenant_id}/pruebas", response_model=PruebasTopeDeKOut)
async def leer_tope(tenant_id: uuid.UUID, principal: Annotated[Principal, Depends(get_principal)]) -> PruebasTopeDeKOut:
    """Tope de `k` de las pruebas del negocio. Solo el master."""
    from core.pruebas import config
    from core.shared.tenant import tenant_scope

    _solo_master(principal)
    try:
        async with tenant_scope(tenant_id):
            propio, efectivo = await config.leer_k_maximo(tenant_id)
    except config.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return PruebasTopeDeKOut(k_maximo=efectivo, k_maximo_propio=propio, k_maximo_de_plataforma=get_settings().pruebas_k_maximo)


@plataforma_router.put("/tenants/{tenant_id}/pruebas", response_model=PruebasTopeDeKOut)
async def fijar_tope(
    tenant_id: uuid.UUID, payload: PruebasTopeDeKIn, principal: Annotated[Principal, Depends(get_principal)]
) -> PruebasTopeDeKOut:
    """Baja o sube el tope de `k` de UN negocio (auditado como `pruebas_k_maximo`); `null` lo devuelve al de
    plataforma. Idempotente. Solo el master."""
    from core.pruebas import config
    from core.shared.tenant import tenant_scope

    _solo_master(principal)
    try:
        async with tenant_scope(tenant_id):
            await config.fijar_k_maximo(tenant_id, payload.k_maximo, actor=f"admin:{principal.admin.email}")
            propio, efectivo = await config.leer_k_maximo(tenant_id)
    except config.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return PruebasTopeDeKOut(k_maximo=efectivo, k_maximo_propio=propio, k_maximo_de_plataforma=get_settings().pruebas_k_maximo)


__all__ = ["plataforma_router", "router"]

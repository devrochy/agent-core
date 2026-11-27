"""Informe de una corrida de pruebas, arreglo sugerido y volver a probar las fallidas (HU-AOS-104, SPEC-53, ADR-0212 §6).

Bajo `/api/v1/admin/agents/{agent_id}/pruebas`, con la sección `agents` (leer: lectura; el resto exige escritura, y un
colaborador sin permiso recibe 403). Sin `PRUEBAS_AGENTE_ENABLED` todo responde 404. Un agente, corrida o resultado de
otro negocio es 404 sin eco.

- `GET …/corridas/{run_id}` — estado, progreso, créditos (estimados, reales y cuánto cubre la cuota gratis), resumen con
  el porcentaje y, por escenario, sus intentos con transcripción, criterio incumplido, razón del juez y arreglo + diff.
- `GET …/resultados/{result_id}/arreglo` — el arreglo con su diff contra el borrador de ahora (no escribe).
- `POST …/resultados/{result_id}/aplicar` `{confirmo: true, lock_version}` — escribe el arreglo en el borrador del agente,
  **sin publicar**. `confirmo` y el `lock_version` que el dueño vio son la confirmación explícita. Errores: 404, 409
  `resultado_no_es_falla`, `sin_arreglo`, `arreglo_ya_aplicado`, `arreglo_no_aplicable` (una respuesta o un dato: se
  editan a mano; `details.seccion`), `sin_borrador`, `stale_lock_version`; 422 `arreglo_invalido` (por campo).
- `POST …/resultados/{result_id}/juez-equivocado` `{juez_equivocado?}` — «Esto no es una falla»: el resultado sale del
  porcentaje y de volver a probar. 409 `resultado_no_es_falla` si no falló.
- `POST …/corridas/{run_id}/volver-a-probar/estimado` y `POST …/corridas/{run_id}/volver-a-probar` `{k, confirmo_creditos}`
  — el estimado y la corrida nueva solo con los escenarios que fallaron, contra el borrador de ahora. Misma compuerta del
  dinero que `POST …/corridas` (409 `corrida_en_curso`, `estimado_cambio`, `plan_limit_creditos`; 422 `k_fuera_de_rango`);
  además 422 `sin_fallidas`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from core.admin.roles import actua_como_master
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_error
from core.api.routers.agents import _catalogo
from core.api.routers.pruebas_corridas import (
    PruebasCorridaOut,
    PruebasEstimadoOut,
    _encolar,
    _estimado_out,
    _habilitado,
    _tenant,
)
from core.billing.enforcement import QuotaExceeded
from core.pruebas import informe as svc
from core.pruebas.corridas import ErrorPruebas

router = APIRouter(prefix="/admin/agents", tags=["admin · pruebas del agente"])


# ------------------------------------------------------------------------------------------------------ salidas
class PruebasReglaOut(BaseModel):
    name: str
    kind: str
    action: str
    phrases: list[str] = Field(default_factory=list)
    tool: str | None = None
    value: int | None = None


class PruebasCambioOut(BaseModel):
    ruta: str = Field(description="Dónde cambia el borrador, p. ej. `guardrails[2]`.")
    antes: Any | None = Field(description="Lo que había (`null` si es nuevo).")
    despues: Any = Field(description="Lo que quedará.")


class PruebasArregloOut(BaseModel):
    tipo: Literal["regla", "respuesta", "dato"]
    porque: str
    regla: PruebasReglaOut | None = Field(default=None, description="Solo en `regla`.")
    texto: str | None = Field(default=None, description="La respuesta o el dato que falta (`respuesta` y `dato`).")
    procedimiento: str | None = None
    seccion: str | None = Field(default=None, description="Dónde se edita un `dato`.")
    aplicable: bool = Field(description="Si «Aplicar al borrador» puede escribirlo ahora.")
    motivo_no_aplicable: str | None = Field(
        default=None, description="`tipo` (se edita a mano), `sin_borrador` o `ya_aplicado`."
    )
    diff: list[PruebasCambioOut] = Field(description="Lo que cambiaría en el borrador de AHORA; vacío si no aplica.")


class PruebasTurnoOut(BaseModel):
    quien: Literal["cliente", "agente"]
    texto: str


class PruebasIntentoOut(BaseModel):
    result_id: uuid.UUID
    intento: int
    estado: Literal["pasa", "falla", "no_se_pudo_probar"]
    fin: str | None
    criterio_incumplido: str | None
    razon: str | None
    transcripcion: list[PruebasTurnoOut]
    tools_llamadas: list[str]
    arreglo: PruebasArregloOut | None
    arreglo_aplicado_en: datetime | None
    juez_equivocado: bool


class PruebasEscenarioInformeOut(BaseModel):
    case_id: uuid.UUID
    titulo: str
    intentos: list[PruebasIntentoOut]


class PruebasProgresoOut(BaseModel):
    hechos: int
    total: int


class PruebasResumenOut(BaseModel):
    pasan: int
    fallan: int = Field(description="Fallas vigentes: sin las marcadas `juez_equivocado`.")
    no_se_pudo_probar: int
    juez_equivocado: int
    porcentaje: int | None = Field(description="Pasan sobre lo que se pudo juzgar; `null` si no hay nada que juzgar.")


class PruebasInformeOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    estado: str
    k: int
    reintento_de: uuid.UUID | None = Field(description="La corrida de la que esta repite las fallidas.")
    version_probada: str | None = Field(description="`borrador`.")
    iniciada_en: datetime | None
    terminada_en: datetime | None
    creditos_estimados: float
    creditos_reales: float
    creditos_reales_de_cuota: float = Field(description="De lo gastado, lo que cubre la cuota gratis de pruebas.")
    creditos_reales_del_saldo: float
    tope_creditos: float
    progreso: PruebasProgresoOut
    resumen: PruebasResumenOut
    borrador_lock_version: int | None = Field(description="El que hay que mandar al aplicar un arreglo.")
    escenarios: list[PruebasEscenarioInformeOut]


class PruebasArregloVistaOut(PruebasArregloOut):
    borrador_lock_version: int | None


class PruebasAplicarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmo: Literal[True] = Field(description="Confirmación explícita del dueño: vio el cambio y quiere aplicarlo.")
    lock_version: int = Field(ge=0, lt=2**31, description="El `borrador_lock_version` que vio al mirar el cambio.")


class PruebasAplicadoOut(BaseModel):
    result_id: uuid.UUID
    arreglo_aplicado_en: datetime
    borrador_lock_version: int
    cambios: list[PruebasCambioOut] = Field(description="Vacío si el borrador ya lo tenía.")
    publicado: Literal[False] = Field(default=False, description="Aplicar nunca publica: el agente sigue con su versión.")


class PruebasJuezEquivocadoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    juez_equivocado: bool = Field(default=True, description="`false` revierte la marca.")


class PruebasJuezEquivocadoOut(BaseModel):
    result_id: uuid.UUID
    estado: str
    juez_equivocado: bool


class PruebasVolverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: int = Field(description="Intentos por escenario: de 1 al tope del negocio.")
    confirmo_creditos: int = Field(ge=0, description="El `estimado` de ese `k` para las fallidas (409 `estimado_cambio` si cambió).")


_ERRORES: dict[int | str, dict[str, str]] = {
    403: {"description": "Sin permiso de escritura en la sección `agents`."},
    404: {"description": "Agente, corrida o resultado inexistente o de otro negocio, o pruebas apagadas."},
}


def _error(exc: ErrorPruebas) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, fields=exc.fields, details=exc.details)


def _arreglo_out(v: dict[str, Any] | None) -> PruebasArregloOut | None:
    return None if v is None else PruebasArregloOut.model_validate(v)


# ------------------------------------------------------------------------------------------------------- rutas
@router.get(
    "/{agent_id}/pruebas/corridas/{run_id}", response_model=PruebasInformeOut, responses=_ERRORES
)
async def informe(
    agent_id: uuid.UUID, run_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents"))]
) -> PruebasInformeOut:
    """El informe de la corrida: progreso, créditos, porcentaje y, por escenario, la conversación, la razón del juez y
    el arreglo sugerido con su diff. Se puede sondear mientras corre."""
    _habilitado()
    try:
        return PruebasInformeOut.model_validate(await svc.informe(_tenant(principal), agent_id, run_id))
    except ErrorPruebas as exc:
        raise _error(exc) from exc


@router.get(
    "/{agent_id}/pruebas/resultados/{result_id}/arreglo", response_model=PruebasArregloVistaOut, responses=_ERRORES
)
async def ver_arreglo(
    agent_id: uuid.UUID, result_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents"))]
) -> PruebasArregloVistaOut:
    """El arreglo de un resultado con el diff contra el borrador de ahora. No escribe nada."""
    _habilitado()
    try:
        return PruebasArregloVistaOut.model_validate(await svc.vista_previa(_tenant(principal), agent_id, result_id))
    except ErrorPruebas as exc:
        raise _error(exc) from exc


@router.post(
    "/{agent_id}/pruebas/resultados/{result_id}/aplicar",
    response_model=PruebasAplicadoOut,
    responses={
        **_ERRORES,
        409: {
            "description": "`resultado_no_es_falla`, `sin_arreglo`, `arreglo_ya_aplicado`, `arreglo_no_aplicable` "
            "(`details = {tipo, seccion, texto}`: se edita a mano), `sin_borrador`, `agente_archivado` o "
            "`stale_lock_version` (`details.lock_version` trae el vigente)."
        },
        422: {"description": "`arreglo_invalido`: el cambio no pasa el contrato del borrador (`fields` por campo)."},
    },
)
async def aplicar_arreglo(
    agent_id: uuid.UUID,
    result_id: uuid.UUID,
    payload: PruebasAplicarIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> PruebasAplicadoOut:
    """Aplica el arreglo al BORRADOR del agente, con la confirmación explícita del dueño. Nunca publica."""
    _habilitado()
    try:
        r = await svc.aplicar(
            _tenant(principal),
            agent_id,
            result_id,
            lock_version=payload.lock_version,
            actor_id=principal.admin.id,
            actor=f"admin:{principal.admin.email}",
            catalogo=_catalogo(),
        )
    except ErrorPruebas as exc:
        raise _error(exc) from exc
    return PruebasAplicadoOut.model_validate(r)


@router.post(
    "/{agent_id}/pruebas/resultados/{result_id}/juez-equivocado",
    response_model=PruebasJuezEquivocadoOut,
    responses={**_ERRORES, 409: {"description": "`resultado_no_es_falla`."}},
)
async def juez_equivocado(
    agent_id: uuid.UUID,
    result_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
    payload: PruebasJuezEquivocadoIn | None = None,
) -> PruebasJuezEquivocadoOut:
    """«Esto no es una falla»: el resultado sale del porcentaje y de volver a probar. Idempotente."""
    _habilitado()
    valor = True if payload is None else payload.juez_equivocado
    try:
        r = await svc.marcar_juez_equivocado(
            _tenant(principal), agent_id, result_id, valor, actor=f"admin:{principal.admin.email}"
        )
    except ErrorPruebas as exc:
        raise _error(exc) from exc
    return PruebasJuezEquivocadoOut(result_id=r.id, estado=r.estado, juez_equivocado=bool(r.juez_equivocado))


@router.post(
    "/{agent_id}/pruebas/corridas/{run_id}/volver-a-probar/estimado",
    response_model=PruebasEstimadoOut,
    responses={**_ERRORES, 409: {"description": "`corrida_en_curso`: la corrida todavía no termina."}, 422: {"description": "`sin_fallidas`."}},
)
async def estimar_fallidas(
    agent_id: uuid.UUID, run_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents", write=True))]
) -> PruebasEstimadoOut:
    """Cuánto cuesta repetir solo las pruebas que fallaron, con cada `k`. No escribe."""
    _habilitado()
    try:
        estimado = await svc.estimar_fallidas(_tenant(principal), agent_id, run_id)
    except ErrorPruebas as exc:
        raise _error(exc) from exc
    return _estimado_out(estimado, master=actua_como_master(principal.admin))


@router.post(
    "/{agent_id}/pruebas/corridas/{run_id}/volver-a-probar",
    response_model=PruebasCorridaOut,
    status_code=202,
    responses={
        **_ERRORES,
        409: {"description": "`corrida_en_curso`, `estimado_cambio` o `plan_limit_creditos`; no se creó la corrida."},
        422: {"description": "`sin_fallidas` o `k_fuera_de_rango`."},
        429: {"description": "`corridas_por_dia`."},
    },
)
async def volver_a_probar(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: PruebasVolverIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> PruebasCorridaOut:
    """Nueva corrida solo con los escenarios que fallaron, contra el borrador, con su propio estimado y aprobación."""
    _habilitado()
    tid = _tenant(principal)
    try:
        corrida = await svc.crear_de_fallidas(
            tid,
            agent_id,
            run_id,
            k=payload.k,
            confirmo_creditos=payload.confirmo_creditos,
            pedida_por=principal.admin.id,
            actor=f"admin:{principal.admin.email}",
        )
    except ErrorPruebas as exc:
        raise _error(exc) from exc
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from exc
    await _encolar(tid, corrida.id)
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


__all__ = ["router"]

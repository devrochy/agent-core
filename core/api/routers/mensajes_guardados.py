"""API admin de los mensajes guardados del negocio (ADR-0178, pista D4): `/api/v1/mensajes/guardados`.

Respuestas rápidas reutilizables entre canales. Hoy es SOLO almacenamiento: nada las envía a un cliente. Las reglas
(variables permitidas, saneo, canales, estados) están en `core/admin/mensajes_guardados_service.py`; aquí solo HTTP.

Autorización (la misma que las plantillas con opciones, `plantillas_opciones.py`): lectura con la sección
`connections`; escritura (crear, editar, borrar) con `require("connections", write=True)` más `require_owner_or_master`.
A diferencia de ellas NO hay bandera por negocio: la bandera `opciones_directas` es de la ejecución directa y esto no
ejecuta nada. Una clave de otro negocio es un 404 `not_found` igual que una inexistente: la consulta filtra por
`tenant_id` además de la RLS.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from core.admin import mensajes_guardados_service as svc
from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError

router = APIRouter(prefix="/mensajes/guardados", tags=["mensajes"])

EstadoGuardado = Literal["borrador", "activo", "archivado"]


# ------------------------------------------------------------------ dependencias
def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def lectura(principal: Annotated[Principal, Depends(require("connections"))]) -> Principal:
    return principal


async def escritura(principal: Annotated[Principal, Depends(require("connections", write=True))]) -> Principal:
    require_owner_or_master(principal)
    return principal


def _api(exc: svc.ErrorMensajeGuardado) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, details=exc.details)


# ------------------------------------------------------------------ esquemas
class MensajeGuardadoOut(BaseModel):
    clave: str
    titulo: str
    texto: str = Field(description="Con variables `{cliente.nombre}`; conserva los saltos de línea.")
    canales: list[str] = Field(description="`ConnectionType.id` donde puede salir; vacío si es para todos.")
    todos_los_canales: bool = Field(description="`canales` vacío: vale para todos los canales que atienden conversaciones.")
    estado: EstadoGuardado
    variables: list[str] = Field(description="Las que cita el texto, en orden y sin repetir.")
    creado_en: datetime
    actualizado_en: datetime


class MensajesGuardadosOut(BaseModel):
    mensajes: list[MensajeGuardadoOut]


class MensajeGuardadoCrearIn(BaseModel):
    clave: str = Field(pattern=r"^[a-z0-9_]{1,40}$", description="Única entre los mensajes vivos del negocio.")
    titulo: str = Field(max_length=svc.MAX_TITULO * 2)
    texto: str = Field(max_length=svc.MAX_TEXTO * 4, description=f"Hasta {svc.MAX_TEXTO} caracteres; solo variables permitidas.")
    canales: list[str] = Field(
        default_factory=list, max_length=20,
        description="`ConnectionType.id` donde puede salir. Vacío o `todos`: todos los canales.",
    )
    estado: Literal["borrador", "activo"] = "borrador"


class MensajeGuardadoEditarIn(BaseModel):
    """Solo cambian los campos que llegan. La clave no se cambia."""

    titulo: str | None = Field(default=None, max_length=svc.MAX_TITULO * 2)
    texto: str | None = Field(default=None, max_length=svc.MAX_TEXTO * 4)
    canales: list[str] | None = Field(default=None, max_length=20)
    estado: EstadoGuardado | None = None


class VistaPreviaGuardadoIn(BaseModel):
    texto: str = Field(max_length=svc.MAX_TEXTO * 4)
    ejemplos: dict[str, Annotated[str, Field(max_length=100)]] = Field(
        default_factory=dict, max_length=10, description="Valor de muestra por variable; si falta se usa uno genérico."
    )


class VistaPreviaGuardadoOut(BaseModel):
    texto_renderizado: str
    variables: list[str]
    ejemplos_usados: dict[str, str] = Field(description="El valor de muestra que se usó por variable.")


class VariablePermitidaOut(BaseModel):
    ruta: str
    descripcion: str


class OpcionesGuardadoOut(BaseModel):
    """Lo que el formulario de edición ofrece: canales y variables válidos."""

    canales: list[str] = Field(description="`ConnectionType.id` que se pueden elegir (más `todos`, que equivale a la lista vacía).")
    variables: list[VariablePermitidaOut]
    max_texto: int
    max_titulo: int


class GuardadoError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class GuardadoErrorOut(BaseModel):
    """El sobre de error de la API (`{error: {code, message, details?}}`). En un 422, `details` trae los valores
    válidos (`permitidas`) y, según el caso, los inválidos (`desconocidas`, `invalidos`)."""

    error: GuardadoError


_ERRORES: dict[int | str, dict[str, Any]] = {404: {"model": GuardadoErrorOut}, 409: {"model": GuardadoErrorOut}, 422: {"model": GuardadoErrorOut}}


# ------------------------------------------------------------------ rutas sin clave (antes de `/{clave}`; con guion, no son una clave)
@router.get("/catalogo-opciones", response_model=OpcionesGuardadoOut)
async def catalogo_opciones(principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    """Canales y variables que el formulario puede ofrecer, y los topes de largo."""
    del principal
    return {
        "canales": svc.canales_permitidos(),
        "variables": [{"ruta": r, "descripcion": d} for r, d in svc.descripciones_de_variables().items()],
        "max_texto": svc.MAX_TEXTO,
        "max_titulo": svc.MAX_TITULO,
    }


@router.post("/vista-previa", response_model=VistaPreviaGuardadoOut, responses=_ERRORES)
async def vista_previa(body: VistaPreviaGuardadoIn, principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    """El texto con valores de muestra, sin guardar ni enviar nada. 422 con el mismo código que al guardar si el texto
    no es válido (`texto_invalido`, `variable_no_permitida`)."""
    del principal
    try:
        return svc.vista_previa(body.texto, body.ejemplos)
    except svc.ErrorMensajeGuardado as exc:
        raise _api(exc) from exc


# ------------------------------------------------------------------ CRUD
@router.get("", response_model=MensajesGuardadosOut, responses={422: {"model": GuardadoErrorOut}})
async def listar(
    principal: Annotated[Principal, Depends(lectura)],
    estado: Annotated[EstadoGuardado | None, Query(description="Filtra por estado; sin él, todos los vivos.")] = None,
) -> dict[str, Any]:
    try:
        return {"mensajes": await svc.listar(_negocio(principal), estado)}
    except svc.ErrorMensajeGuardado as exc:
        raise _api(exc) from exc


@router.post("", response_model=MensajeGuardadoOut, status_code=201, responses=_ERRORES)
async def crear(body: MensajeGuardadoCrearIn, principal: Annotated[Principal, Depends(escritura)]) -> dict[str, Any]:
    """Crea un mensaje guardado. 409 `clave_repetida` si el negocio ya tiene uno vivo con esa clave; 409
    `limite_de_mensajes` al pasar el tope; 422 por texto, variables, canales o estado inválidos."""
    try:
        return await svc.crear(
            _negocio(principal), principal.admin.id, body.clave, body.titulo, body.texto, body.canales, body.estado
        )
    except svc.ErrorMensajeGuardado as exc:
        raise _api(exc) from exc


@router.get("/{clave}", response_model=MensajeGuardadoOut, responses={404: {"model": GuardadoErrorOut}})
async def obtener(clave: str, principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    try:
        return await svc.obtener(_negocio(principal), clave)
    except svc.ErrorMensajeGuardado as exc:
        raise _api(exc) from exc


@router.patch("/{clave}", response_model=MensajeGuardadoOut, responses=_ERRORES)
async def editar(clave: str, body: MensajeGuardadoEditarIn, principal: Annotated[Principal, Depends(escritura)]) -> dict[str, Any]:
    """Cambia solo los campos que llegan; `estado: archivado` lo archiva y `activo` lo reactiva. 422 `sin_cambios` si
    no llega ninguno."""
    try:
        return await svc.editar(
            _negocio(principal), principal.admin.id, clave,
            titulo=body.titulo, texto=body.texto, canales=body.canales, estado=body.estado,
        )
    except svc.ErrorMensajeGuardado as exc:
        raise _api(exc) from exc


@router.delete("/{clave}", status_code=204, responses={404: {"model": GuardadoErrorOut}})
async def eliminar(clave: str, principal: Annotated[Principal, Depends(escritura)]) -> Response:
    """Borra (lógicamente) el mensaje y libera su clave."""
    try:
        await svc.eliminar(_negocio(principal), principal.admin.id, clave)
    except svc.ErrorMensajeGuardado as exc:
        raise _api(exc) from exc
    return Response(status_code=204)

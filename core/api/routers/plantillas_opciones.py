"""API admin de plantillas con opciones (SPEC-40, ADR-0172, HU-OPC-05): `/api/v1/plantillas-opciones`.

El dueño crea, valida, previsualiza, publica, retira y prueba sus plantillas con botones que ejecutan una tool sin
LLM. Las reglas están en `core/admin/plantillas_opciones_service.py`; aquí solo HTTP.

Autorización (como `meta_ops.py`): lectura con la sección `connections`; escritura (crear, guardar, borrar,
publicar, retirar y enviar prueba) con `require("connections", write=True)` más `require_owner_or_master`. DESPUÉS
del permiso, la bandera por negocio `opciones_directas.habilitada` (apagada por defecto, la fija el master): sin
ella TODAS las rutas responden 404 `opciones_directas_no_habilitadas`, sin tocar ni devolver datos. La clave de
otro negocio es un 404 `not_found` igual que una inexistente: la consulta filtra por `tenant_id` además de la RLS.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from core.admin import plantillas_opciones_service as svc
from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.db.session import get_session
from core.kernel.opciones import bandera
from core.kernel.opciones.definicion import Definicion

router = APIRouter(prefix="/plantillas-opciones", tags=["plantillas con opciones"])

NO_HABILITADO = "opciones_directas_no_habilitadas"


# ------------------------------------------------------------------ dependencias
def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _con_bandera(principal: Principal) -> Principal:
    tenant_id = _negocio(principal)
    async with get_session() as session:
        if not await bandera.habilitada(session, tenant_id):
            raise ApiError(404, NO_HABILITADO, "Las opciones directas no están habilitadas en este negocio.")
    return principal


async def lectura(principal: Annotated[Principal, Depends(require("connections"))]) -> Principal:
    return await _con_bandera(principal)


async def escritura(principal: Annotated[Principal, Depends(require("connections", write=True))]) -> Principal:
    require_owner_or_master(principal)
    return await _con_bandera(principal)


def _actor(principal: Principal) -> str:
    return f"admin:{principal.admin.id}"


def _api(exc: svc.ErrorPlantillaOpciones) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, details=exc.details)


# ------------------------------------------------------------------ esquemas
class OpcionesHallazgoOut(BaseModel):
    path: str = Field(description="Dónde está la falla, p. ej. `opciones[1].accion.tool`.")
    code: str
    severity: Literal["error", "warning"] = Field(description="Un `error` impide publicar; un `warning` solo informa.")
    mensaje: str


class OpcionesVersionOut(BaseModel):
    version_n: int
    publicada_en: datetime
    retirada_en: datetime | None
    definition_hash: str
    message_template_id: uuid.UUID | None = Field(description="Plantilla de Meta de esta versión; la fija la publicación a Meta.")
    vigente: bool


class OpcionesPlantillaResumen(BaseModel):
    clave: str
    nombre: str
    estado: Literal["draft", "published", "retired"]
    version_vigente: int | None
    tiene_borrador_sin_publicar: bool
    creada_en: datetime
    actualizada_en: datetime


class OpcionesPlantillaOut(OpcionesPlantillaResumen):
    borrador: Definicion | None
    definicion_vigente: Definicion | None
    versiones: list[OpcionesVersionOut]


class OpcionesPlantillasOut(BaseModel):
    plantillas: list[OpcionesPlantillaResumen]


class OpcionesCrearIn(BaseModel):
    clave: str = Field(pattern=r"^[a-z0-9_]{1,40}$", description="Única entre las plantillas vivas del negocio.")
    nombre: str = Field(default="", max_length=svc.MAX_NOMBRE)
    definicion: dict[str, Any] | None = Field(default=None, description="Borrador inicial (forma de `Definicion`).")


class OpcionesBorradorIn(BaseModel):
    nombre: str | None = Field(default=None, max_length=svc.MAX_NOMBRE)
    definicion: dict[str, Any] = Field(description="Reemplaza el borrador. Solo se valida la forma; el lint completo corre al publicar.")


class OpcionesDefinicionIn(BaseModel):
    definicion: dict[str, Any]


class VistaPreviaOpcionesIn(BaseModel):
    definicion: dict[str, Any]
    ejemplos: dict[str, Annotated[str, Field(max_length=100)]] = Field(
        default_factory=dict, max_length=30, description="Valor de muestra por ruta de variable; si falta se usa uno genérico."
    )


class OpcionesValidacionOut(BaseModel):
    valida: bool = Field(description="Sin hallazgos de severidad `error`.")
    hallazgos: list[OpcionesHallazgoOut]


class VistaPreviaOpcionesOut(BaseModel):
    interactivo: dict[str, Any] = Field(description="Mensaje interactivo de WhatsApp (ventana de 24 h abierta), con ids de relleno.")
    cabe_en_whatsapp: bool = Field(description="El interactivo cumple los topes de Meta; si no, saldría el texto de respaldo.")
    componentes_meta: list[dict[str, Any]] = Field(description="Componentes de la plantilla de Meta (ventana cerrada).")
    cuerpo_meta_renderizado: str
    texto_de_respaldo: str = Field(description="Canal sin botones: el cuerpo y las opciones numeradas, sin ejecución directa.")
    hallazgos: list[OpcionesHallazgoOut] = Field(description="Solo avisos: con errores la ruta responde 422.")


class OpcionesPublicadaOut(BaseModel):
    clave: str
    version_n: int
    definition_hash: str
    publicada_en: datetime
    message_template_id: uuid.UUID | None = Field(description="Siempre nulo aquí: la publicación a Meta es un paso posterior.")
    hallazgos: list[OpcionesHallazgoOut]


class OpcionesEnviarPruebaIn(BaseModel):
    conversation_id: uuid.UUID = Field(description="Una conversación de este negocio. Nunca un número libre.")
    clave_idempotencia: str = Field(min_length=8, max_length=64, description="Repetir la misma clave no duplica el envío.")


class OpcionesPruebaOut(BaseModel):
    enviado: bool
    ya_enviado: bool = Field(description="La clave de idempotencia ya había enviado este mensaje.")
    via: Literal["interactivo", "plantilla", "texto"] | None
    ejecucion_directa: bool = Field(description="El cliente puede tocar una opción (interactivo o plantilla).")
    degradado: bool
    send_id: uuid.UUID | None


class AccionDelCatalogoOut(BaseModel):
    tool: str
    descripcion: str
    parameters: dict[str, Any] = Field(description="JSON Schema de los argumentos: de ahí sale el formulario de `args_fijos`.")
    sensible: bool
    confirmacion_obligatoria: bool = Field(description="El cliente confirmará (`optc`/`optn`) aunque la opción no lo pida.")


class OpcionesAccionesOut(BaseModel):
    acciones: list[AccionDelCatalogoOut]


class OpcionesVariableOut(BaseModel):
    ruta: str
    descripcion: str
    origen: Literal["conversacion", "servidor"]


class OpcionesVariablesOut(BaseModel):
    variables: list[OpcionesVariableOut]


# ------------------------------------------------------------------ catálogos y herramientas sin clave
# (van antes de las rutas con `/{clave}` por claridad; los nombres con guion no pueden ser una clave)
@router.get("/catalogo-acciones", response_model=OpcionesAccionesOut)
async def catalogo_acciones(principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    """Las tools que el negocio puede usar como acción de una opción: elegibles (marca de su autor, pack activo,
    binding explícito y habilitado, sin gobernar por un procedimiento), con su JSON Schema y si son sensibles."""
    return {"acciones": await svc.catalogo_acciones(_negocio(principal))}


@router.get("/catalogo-variables", response_model=OpcionesVariablesOut)
async def catalogo_variables(principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    """Las variables que el servidor resuelve y el dueño puede citar en el cuerpo (`{cliente.nombre}`) y en
    `args_contexto`."""
    del principal
    return {"variables": svc.catalogo_variables()}


@router.post("/validar", response_model=OpcionesValidacionOut)
async def validar(body: OpcionesDefinicionIn, principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    """Lint de una definición sin guardar nada. Siempre 200: los hallazgos (`error` y `warning`) son el resultado."""
    hallazgos = await svc.validar(_negocio(principal), body.definicion)
    return {"valida": not any(h.severity == "error" for h in hallazgos), "hallazgos": svc.hallazgos_como_dicts(hallazgos)}


@router.post("/vista-previa", response_model=VistaPreviaOpcionesOut)
async def vista_previa(body: VistaPreviaOpcionesIn, principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    """Lo que saldría por cada vía con valores de muestra, sin enviar nada. 422 con los hallazgos si la
    definición tiene errores."""
    try:
        return await svc.vista_previa(_negocio(principal), body.definicion, body.ejemplos)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc


# ------------------------------------------------------------------ CRUD de borradores
@router.get("", response_model=OpcionesPlantillasOut)
async def listar(principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    return {"plantillas": await svc.listar(_negocio(principal))}


@router.post("", response_model=OpcionesPlantillaOut, status_code=201)
async def crear(body: OpcionesCrearIn, principal: Annotated[Principal, Depends(escritura)]) -> dict[str, Any]:
    """Crea un borrador. 409 `clave_repetida` si el negocio ya tiene una plantilla viva con esa clave."""
    try:
        return await svc.crear(_negocio(principal), _actor(principal), body.clave, body.nombre, body.definicion)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc


@router.get("/{clave}", response_model=OpcionesPlantillaOut)
async def obtener(clave: str, principal: Annotated[Principal, Depends(lectura)]) -> dict[str, Any]:
    try:
        return await svc.obtener(_negocio(principal), clave)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc


@router.put("/{clave}/borrador", response_model=OpcionesPlantillaOut)
async def guardar_borrador(clave: str, body: OpcionesBorradorIn, principal: Annotated[Principal, Depends(escritura)]) -> dict[str, Any]:
    """Reemplaza el borrador. Las versiones publicadas no cambian. 422 `definicion_invalida` si la forma falla."""
    try:
        return await svc.guardar_borrador(_negocio(principal), _actor(principal), clave, body.nombre, body.definicion)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc


@router.delete("/{clave}", status_code=204)
async def eliminar(clave: str, principal: Annotated[Principal, Depends(escritura)]) -> Response:
    """Borra (lógicamente) una plantilla que nunca se publicó o ya está retirada y libera su clave. 409 si está publicada."""
    try:
        await svc.eliminar(_negocio(principal), _actor(principal), clave)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc
    return Response(status_code=204)


# ------------------------------------------------------------------ ciclo de vida
@router.post("/{clave}/publicar", response_model=OpcionesPublicadaOut, status_code=201)
async def publicar(clave: str, principal: Annotated[Principal, Depends(escritura)]) -> dict[str, Any]:
    """Congela el borrador como la versión siguiente (inmutable). 422 `definicion_con_errores` con los hallazgos si
    falla el lint: no cambia nada. 409 `sin_cambios` si el borrador es igual a la versión vigente."""
    try:
        return await svc.publicar(_negocio(principal), _actor(principal), clave)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc


@router.post("/{clave}/retirar", response_model=OpcionesPlantillaOut)
async def retirar(clave: str, principal: Annotated[Principal, Depends(escritura)]) -> dict[str, Any]:
    """Retira la plantilla publicada: deja de enviarse y sus botones en vuelo responden «ya no está disponible»."""
    try:
        return await svc.retirar(_negocio(principal), _actor(principal), clave)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc


@router.post("/{clave}/enviar-prueba", response_model=OpcionesPruebaOut)
async def enviar_prueba(clave: str, body: OpcionesEnviarPruebaIn, principal: Annotated[Principal, Depends(escritura)]) -> dict[str, Any]:
    """Envía la versión vigente a UNA conversación del negocio (nunca a un número libre), por la vía que
    corresponda. Es un mensaje real a ese cliente: el panel debe pedir confirmación."""
    try:
        return await svc.enviar_prueba(_negocio(principal), _actor(principal), clave, body.conversation_id, body.clave_idempotencia)
    except svc.ErrorPlantillaOpciones as exc:
        raise _api(exc) from exc

"""Datos de prueba por agente (HU-QA-10, ADR-0146). Solo en test.

- `GET /api/v1/admin/agents/{agent_id}/datos-de-prueba` — si el agente tiene datos que generar,
  si falta la descripción del negocio y el último lote (origen y conteos por tipo).
- `POST …` — genera según la descripción del negocio (LLM con plantilla de respaldo), reemplaza
  el lote anterior del agente y siembra el nuevo. 409 `en_curso` con otra generación del mismo
  agente; 409 `sin_datos` si el agente no tiene forma; 422 `descripcion_requerida`.
- `DELETE …` — retira los datos de prueba del agente (borrado lógico en el pack).
- `GET …/importar-datos` (+ `/plantilla.csv`), `POST …/importar-datos/validar`, `POST …/importar-datos` y
  `DELETE …/importar-datos` — importar datos existentes desde un CSV (HU-QA-12, ADR-0180). Escribir
  exige además escritura en la sección del pack del agente. La vista previa no escribe nada.

Sin `QA_TESTERS_ENABLED` las tres responden 404: en producción la función no existe. Permiso:
sección `agents` (escritura para generar o borrar). El agente se busca con el `tenant_id` de la
sesión además de la RLS: uno de otro negocio es 404 sin eco.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import BaseModel, Field

from core.api.deps import Principal, effective_access, get_principal, require
from core.api.errors import ApiError
from core.config import get_settings
from core.qa.contrato_importacion import TOPE_BYTES

router = APIRouter(prefix="/admin/agents", tags=["admin · agentes"])
modulos_router = APIRouter(prefix="/admin/datos-de-prueba", tags=["admin · datos de prueba"])



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


class EstadoDatosDePruebaModulo(BaseModel):
    modulo: str
    titulo: str
    disponible: bool
    motivo: str | None = Field(default=None, description="Por qué el módulo no tiene datos que generar.")
    falta_descripcion: bool = Field(description="El negocio no tiene descripción propia: hay que escribirla antes.")
    lote: LoteDePrueba | None


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.not_found()
    return principal.tenant_id


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
        return await svc.estado(_tenant(principal), agent_id)
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
        return await svc.generar(_tenant(principal), agent_id)
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
        return await svc.borrar(_tenant(principal), agent_id)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


# ------------------------------------------------------------------ por módulo del negocio
async def _permiso_modulo(principal: Principal, modulo: str, *, write: bool) -> None:
    from core.qa.datos_generados import MODULOS

    definicion = MODULOS.get(modulo)
    if definicion is None:
        raise ApiError.not_found()
    if principal.admin.role == "master":
        return
    acceso = await effective_access(principal.admin, definicion.seccion, principal.tenant_id)
    if acceso is None or (write and acceso != "write"):
        raise ApiError.forbidden(f"Sin permiso para la sección '{definicion.seccion}'.")


@modulos_router.get("/{modulo}", response_model=EstadoDatosDePruebaModulo)
async def estado_modulo(
    modulo: str, principal: Annotated[Principal, Depends(get_principal)]
) -> dict[str, object]:
    """Estado de los datos de prueba del módulo (Ventas, Agenda, Cobranza o Suscripciones)."""
    from core.qa import datos_generados as svc

    _habilitado()
    await _permiso_modulo(principal, modulo, write=True)
    try:
        return await svc.estado_modulo(_tenant(principal), modulo)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@modulos_router.post("/{modulo}", response_model=LoteGenerado, status_code=201)
async def generar_modulo(
    modulo: str, principal: Annotated[Principal, Depends(get_principal)]
) -> dict[str, object]:
    """Genera los datos del módulo según la descripción del negocio y reemplaza el lote anterior."""
    from core.qa import datos_generados as svc

    _habilitado()
    await _permiso_modulo(principal, modulo, write=True)
    try:
        return await svc.generar_modulo(_tenant(principal), modulo)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@modulos_router.delete("/{modulo}", response_model=DatosRetirados)
async def borrar_modulo(
    modulo: str, principal: Annotated[Principal, Depends(get_principal)]
) -> dict[str, object]:
    """Retira todos los datos de prueba del módulo; lo creado a mano no se toca."""
    from core.qa import datos_generados as svc

    _habilitado()
    await _permiso_modulo(principal, modulo, write=True)
    try:
        return await svc.borrar_modulo(_tenant(principal), modulo)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


# ------------------------------------------------------------------ importar datos existentes (HU-QA-12)
class ColumnaOut(BaseModel):
    nombre: str
    descripcion: str
    obligatoria: bool


class PlantillaImportacionOut(BaseModel):
    id: str
    titulo: str
    ayuda: str
    columna_clave: str
    columnas: list[ColumnaOut]
    tope_filas: int
    tope_bytes: int


class LoteImportado(BaseModel):
    lotes: int
    creado_en: datetime
    conteos: dict[str, int] = Field(description="Registros importados por tipo (`producto`, `cliente`, `cuota`…).")


class EstadoImportacion(BaseModel):
    disponible: bool
    motivo: str | None = Field(default=None, description="Por qué el agente no importa datos.")
    plantilla: PlantillaImportacionOut | None
    importado: LoteImportado | None


class ErrorDeFila(BaseModel):
    fila: int = Field(description="Número de fila en el archivo (la cabecera es la 1).")
    columna: str | None
    mensaje: str


class VistaPreviaImportacion(BaseModel):
    total_filas: int
    nuevas: int = Field(description="Filas que se crearían.")
    existentes: int = Field(description="Filas cuya clave ya existe: se omiten.")
    con_errores: int
    errores: list[ErrorDeFila] = Field(description="Hasta 50; el total está en `errores_total`.")
    errores_total: int
    columnas_ignoradas: list[str] = Field(description="Cabeceras del archivo que no son de la plantilla.")
    columnas_faltantes: list[str] = Field(description="Columnas obligatorias que el archivo no trae.")


class ImportacionHecha(BaseModel):
    lote_id: uuid.UUID
    plantilla: str
    creado_en: datetime
    creadas: int
    existentes: int
    con_errores: int
    conteos: dict[str, int]


def _actor(principal: Principal) -> str:
    return f"admin:{principal.admin.email}"


async def _permiso_importar(principal: Principal, agent_id: uuid.UUID, *, escribir: bool) -> None:
    """Sección `agents` (la de la ruta) y la del pack del agente: lo que el tester no pueda tocar a mano
    tampoco lo importa por aquí."""
    from core.qa import importar_datos as svc

    try:
        d = await svc.destino(_tenant(principal), agent_id)
    except svc.ErrorDatosDePrueba as exc:  # agente inexistente o de otro negocio: 404 sin eco
        raise _error(exc) from exc
    if isinstance(d, str):
        return  # el servicio responde 409 sin_plantilla (o 404 si el agente no es del negocio)
    if principal.admin.role == "master":
        return
    acceso = await effective_access(principal.admin, d.modulo.seccion, principal.tenant_id)
    if acceso is None or (escribir and acceso != "write"):
        raise ApiError.forbidden(f"Sin permiso para la sección '{d.modulo.seccion}'.")


async def _leer_archivo(archivo: UploadFile) -> bytes:
    contenido = await archivo.read(TOPE_BYTES + 1)
    if len(contenido) > TOPE_BYTES:
        raise ApiError(413, "archivo_muy_grande", f"El archivo pesa más de {TOPE_BYTES // 1_000_000} MB. Divídelo en partes.")
    return contenido


@router.get("/{agent_id}/importar-datos", response_model=EstadoImportacion)
async def estado_importacion(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents"))]
) -> dict[str, object]:
    """Si el agente importa datos, su plantilla (columnas y topes) y lo ya importado."""
    from core.qa import importar_datos as svc

    _habilitado()
    try:
        return await svc.estado(_tenant(principal), agent_id)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.get("/{agent_id}/importar-datos/plantilla.csv")
async def plantilla_importacion(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents"))]
) -> Response:
    """La plantilla CSV del agente: cabeceras y filas de ejemplo (UTF-8 con BOM, para Excel)."""
    from core.qa import importar_datos as svc

    _habilitado()
    try:
        nombre, contenido = await svc.plantilla_del_agente(_tenant(principal), agent_id)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc
    return Response(
        content=contenido,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.post("/{agent_id}/importar-datos/validar", response_model=VistaPreviaImportacion)
async def validar_importacion(
    agent_id: uuid.UUID,
    archivo: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> dict[str, object]:
    """Vista previa del archivo: errores por fila y cuántas filas se crearían o ya existen. No escribe."""
    from core.qa import importar_datos as svc

    _habilitado()
    await _permiso_importar(principal, agent_id, escribir=True)
    contenido = await _leer_archivo(archivo)
    try:
        return await svc.previsualizar(_tenant(principal), agent_id, contenido)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.post("/{agent_id}/importar-datos", response_model=ImportacionHecha, status_code=201)
async def importar_datos(
    agent_id: uuid.UUID,
    archivo: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require("agents", write=True))],
    solo_validas: Annotated[bool, Form()] = False,
) -> dict[str, object]:
    """Importa el archivo. Con errores en alguna fila responde 422 `archivo_con_errores` salvo `solo_validas`.
    Las filas cuya clave ya existe se omiten (idempotente)."""
    from core.qa import importar_datos as svc

    _habilitado()
    await _permiso_importar(principal, agent_id, escribir=True)
    contenido = await _leer_archivo(archivo)
    try:
        return await svc.importar(
            _tenant(principal), agent_id, contenido, solo_validas=solo_validas, actor=_actor(principal)
        )
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.delete("/{agent_id}/importar-datos", response_model=DatosRetirados)
async def borrar_importacion(
    agent_id: uuid.UUID, principal: Annotated[Principal, Depends(require("agents", write=True))]
) -> dict[str, object]:
    """Retira solo lo que se importó para este agente; lo generado y lo creado a mano no se tocan."""
    from core.qa import importar_datos as svc

    _habilitado()
    await _permiso_importar(principal, agent_id, escribir=True)
    try:
        return await svc.borrar(_tenant(principal), agent_id, actor=_actor(principal))
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc

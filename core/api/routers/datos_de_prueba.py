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
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from core.api.deps import Principal, effective_access, get_principal, require
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_responses
from core.config import get_settings
from core.qa.contrato_importacion import TOPE_BYTES, TOPE_FILAS

router = APIRouter(prefix="/admin/agents", tags=["admin · agentes"])
modulos_router = APIRouter(prefix="/admin/datos-de-prueba", tags=["admin · datos de prueba"])

TOPE_CELDA = 2000  # caracteres por valor de la precarga (ADR-0194)



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
# En producción (sin `QA_TESTERS_ENABLED`) responden solo si la plantilla del agente se declara
# `en_produccion` (ADR-0194 §2: hoy, `productos`); si no, 404 como antes. La precarga con mapeo de IA
# (`…/importar-datos/mapear`, `…/importar-datos/filas[/validar]`) sigue sin estado en el servidor.
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
    en_produccion: bool = Field(description="La plantilla también se importa fuera de test (ADR-0194).")
    importa_fotos: bool = Field(description="Se puede cargar desde fotos, una foto por fila (ADR-0194).")


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


async def _habilitado_importar(principal: Principal, agent_id: uuid.UUID) -> None:
    """En test, siempre. Fuera de test, solo si la plantilla del agente se declara `en_produccion`
    (ADR-0194 §2); si no, 404 como si la función no existiera."""
    from core.qa import importar_datos as svc

    if get_settings().qa_testers_enabled:
        return
    try:
        d = await svc.destino(_tenant(principal), agent_id)
    except svc.ErrorDatosDePrueba as exc:
        raise ApiError.not_found() from exc
    if isinstance(d, str) or not d.plantilla.en_produccion:
        raise ApiError.not_found()


async def _permiso_importar(principal: Principal, agent_id: uuid.UUID, *, escribir: bool) -> None:
    """Sección `agents` (la de la ruta) y la del pack del agente: lo que el tester no pueda tocar a mano
    tampoco lo importa por aquí."""
    from core.qa import importar_datos as svc

    try:
        d = await svc.destino(_tenant(principal), agent_id)
    except svc.ErrorDatosDePrueba as exc:  # agente inexistente o de otro negocio: 404 sin eco
        raise _error(exc) from exc
    if principal.admin.role == "master":
        return
    # Sin plantilla (el agente cambió o se desactivó el pack) solo queda borrar: se exige escritura en las
    # secciones de lo que se borraría. Importar y validar responden 409 más adelante.
    secciones = {d.modulo.seccion} if not isinstance(d, str) else await svc.secciones_importadas(
        _tenant(principal), agent_id
    )
    for seccion in sorted(secciones):
        acceso = await effective_access(principal.admin, seccion, principal.tenant_id)
        if acceso is None or (escribir and acceso != "write"):
            raise ApiError.forbidden(f"Sin permiso para la sección '{seccion}'.")


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

    await _habilitado_importar(principal, agent_id)
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

    await _habilitado_importar(principal, agent_id)
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

    await _habilitado_importar(principal, agent_id)
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

    await _habilitado_importar(principal, agent_id)
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

    await _habilitado_importar(principal, agent_id)
    await _permiso_importar(principal, agent_id, escribir=True)
    try:
        return await svc.borrar(_tenant(principal), agent_id, actor=_actor(principal))
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


# ------------------------------------------------------------------ precarga con mapeo de IA (ADR-0194)
class AsignacionOut(BaseModel):
    cabecera: str = Field(description="Cabecera tal como viene en el archivo.")
    columna: str | None = Field(description="Columna de la plantilla; `null` = se ignora.")
    origen: Literal["heuristica", "ia", "ninguno"]


class IAMapeoOut(BaseModel):
    usada: bool = Field(description="La IA asignó al menos una columna.")
    motivo_fallo: Literal["proveedor", "formato"] | None = Field(
        default=None, description="Si se llamó a la IA y no ayudó; esas cabeceras quedan sin asignar."
    )


class FilaPrecarga(BaseModel):
    fila: int = Field(ge=1, le=1_000_000, description="Número de fila en el archivo original (la cabecera es la 1).")
    valores: dict[str, str | None] = Field(
        max_length=50, description="Columna de la plantilla → texto, como en el CSV. Lo demás se ignora."
    )


class MapeoPropuesto(BaseModel):
    plantilla: PlantillaImportacionOut
    mapeo: list[AsignacionOut]
    columnas_faltantes: list[str] = Field(description="Obligatorias sin cabecera: el cliente debe completarlas.")
    ia: IAMapeoOut
    filas: list[FilaPrecarga]
    vista_previa: VistaPreviaImportacion


class PrecargaIn(BaseModel):
    filas: list[FilaPrecarga] = Field(min_length=1, max_length=TOPE_FILAS)
    solo_validas: bool = False


TOPE_CUERPO_PRECARGA = 2 * TOPE_BYTES  # el CSV tiene 1 MB; en JSON pesa más por las claves


async def _tope_cuerpo(request: Request) -> None:
    """La precarga no puede pesar más que el CSV que la originó (con margen por el JSON). Se exige
    `Content-Length`: el servidor HTTP no lee más bytes que los declarados, así que acota el cuerpo
    antes de que se parsee entero."""
    declarado = request.headers.get("content-length", "")
    if not (declarado.isascii() and declarado.isdigit()):
        raise ApiError(411, "length_required", "Falta el tamaño del cuerpo (Content-Length).")
    if int(declarado) > TOPE_CUERPO_PRECARGA:
        raise ApiError(
            413, "payload_too_large", f"La precarga pesa más de {TOPE_CUERPO_PRECARGA // 1_000_000} MB. Impórtala en partes."
        )


def _filas(cuerpo: PrecargaIn) -> list[tuple[int, dict[str, str | None]]]:
    for fila in cuerpo.filas:
        for columna, valor in fila.valores.items():
            if len(columna) > 64 or (valor is not None and len(valor) > TOPE_CELDA):
                raise ApiError(422, "valor_muy_largo", f"La fila {fila.fila} tiene un valor demasiado largo.")
    return [(f.fila, f.valores) for f in cuerpo.filas]


@router.post("/{agent_id}/importar-datos/mapear", response_model=MapeoPropuesto)
async def mapear_importacion(
    agent_id: uuid.UUID,
    archivo: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> dict[str, object]:
    """Propone a qué columna de la plantilla va cada cabecera (sinónimos del pack y, para las que no
    reconoce, una llamada a la IA medida en el ledger) y devuelve las filas normalizadas con su vista
    previa. No escribe datos: el cliente edita la precarga y la confirma con `…/importar-datos/filas`."""
    from core.qa import importar_datos as svc
    from core.qa import mapeo_columnas

    await _habilitado_importar(principal, agent_id)
    await _permiso_importar(principal, agent_id, escribir=True)
    contenido = await _leer_archivo(archivo)
    try:
        return await mapeo_columnas.mapear(_tenant(principal), agent_id, contenido)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.post("/{agent_id}/importar-datos/filas/validar", response_model=VistaPreviaImportacion)
async def validar_precarga(
    agent_id: uuid.UUID,
    cuerpo: PrecargaIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
    _tope: Annotated[None, Depends(_tope_cuerpo)] = None,
) -> dict[str, object]:
    """Vista previa de la precarga editada. No escribe."""
    from core.qa import importar_datos as svc

    await _habilitado_importar(principal, agent_id)
    await _permiso_importar(principal, agent_id, escribir=True)
    try:
        return await svc.previsualizar_filas(_tenant(principal), agent_id, _filas(cuerpo))
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.post("/{agent_id}/importar-datos/filas", response_model=ImportacionHecha, status_code=201)
async def importar_precarga(
    agent_id: uuid.UUID,
    cuerpo: PrecargaIn,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
    _tope: Annotated[None, Depends(_tope_cuerpo)] = None,
) -> dict[str, object]:
    """Importa la precarga editada: se vuelve a validar contra el esquema del pack. Con errores en
    alguna fila responde 422 `archivo_con_errores` salvo `solo_validas`. Idempotente por clave natural."""
    from core.qa import importar_datos as svc

    await _habilitado_importar(principal, agent_id)
    await _permiso_importar(principal, agent_id, escribir=True)
    try:
        return await svc.importar_filas(
            _tenant(principal),
            agent_id,
            _filas(cuerpo),
            solo_validas=cuerpo.solo_validas,
            actor=_actor(principal),
        )
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


# ------------------------------------------------------------------ fotos: una foto = una fila (ADR-0194)
class CotizacionFotos(BaseModel):
    fotos: int
    creditos: int = Field(description="Créditos que cuestan todas las fotos (redondeado hacia arriba).")
    por_foto: int
    llave_propia: bool = Field(description="El negocio usa su propia llave de visión: solo se cobra el fee.")
    usado: int = Field(description="Créditos usados en el mes.")
    limite: int | None = Field(description="Créditos del plan al mes; `null` = sin límite.")
    disponible: int | None
    alcanza: bool = Field(description="Si el crédito del mes alcanza para todas las fotos.")


class FilaDeFoto(BaseModel):
    image_key: str
    image_url: str
    valores: dict[str, str] = Field(description="Fila propuesta para la precarga (columna → texto).")
    creditos: Decimal | None = Field(description="Créditos cobrados por esta foto (0 si ya se había procesado).")
    repetida: bool = Field(description="La misma foto ya se había procesado: se respondió sin cobrar otra vez.")


@router.get("/{agent_id}/importar-datos/fotos/cotizacion", response_model=CotizacionFotos)
async def cotizar_fotos(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
    fotos: Annotated[int, Query(ge=1, le=500)] = 1,
) -> dict[str, object]:
    """Cuánto cuesta reconocer `fotos` fotos y si el crédito del mes alcanza. No escribe nada."""
    from core.qa import importar_datos as svc
    from core.qa import importar_fotos

    await _habilitado_importar(principal, agent_id)
    await _permiso_importar(principal, agent_id, escribir=True)
    try:
        return await importar_fotos.cotizacion(_tenant(principal), agent_id, fotos)
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc


@router.post(
    "/{agent_id}/importar-datos/fotos",
    response_model=FilaDeFoto,
    responses=plan_limit_responses("creditos"),
)
async def importar_foto(
    agent_id: uuid.UUID,
    archivo: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> dict[str, object]:
    """Una foto → una fila propuesta para la precarga. Cotiza y verifica el crédito antes de llamar a
    la visión (409 `plan_limit_creditos` si no alcanza, sin cobrar); la misma foto otra vez se
    responde sin cobrar. La fila se confirma con el resto en `…/importar-datos/filas`."""
    from core import almacenamiento
    from core.api.plan_limits import plan_limit_error
    from core.billing.enforcement import QuotaExceeded
    from core.qa import importar_datos as svc
    from core.qa import importar_fotos

    await _habilitado_importar(principal, agent_id)
    await _permiso_importar(principal, agent_id, escribir=True)
    limite = almacenamiento.PERFILES["producto"].max_bytes
    contenido = await archivo.read(limite + 1)
    try:
        return await importar_fotos.procesar_foto(
            _tenant(principal), agent_id, contenido, mimetype=archivo.content_type, nombre=archivo.filename
        )
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from exc
    except svc.ErrorDatosDePrueba as exc:
        raise _error(exc) from exc

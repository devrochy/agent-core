"""Reportes de clientes: enviar y ver los propios (SPEC-54, ADR-0213 §8 y §9, HU-FB-03).

Bajo `/api/v1/feedback`, con cualquier rol del negocio (`get_principal`). Una sola API para el panel y la APK:

- `POST /reports` (multipart: `tipo`, `area`, `titulo`, `descripcion` y hasta 5 `adjuntos`) con la cabecera
  `Idempotency-Key` obligatoria (400 si falta). 201 con el reporte en `recibido`; 200 con el mismo reporte si la
  clave ya se usó (la APK reintenta en redes móviles). Errores: 422 `adjunto_invalido`, `demasiados_adjuntos`,
  `adjunto_repetido`, `tipo_invalido`, `area_invalida`; 413 `adjunto_muy_grande`, `adjuntos_muy_grandes`; 409
  `idempotency_key_en_uso`; 429 con `Retry-After` (5 por hora por usuario, 30 por día por negocio); 503 con
  `Retry-After` si el almacenamiento falla.
- `GET /reports?alcance=mios|negocio&estado=&cursor=&limit=`: los míos; el owner ve los de todo el negocio.
- `GET /reports/{id}`: detalle con URLs firmadas de los adjuntos; 404 si es de otro usuario o de otro negocio.
- `GET /catalogo`: tipos, áreas y límites del formulario.

El cliente nunca recibe el revisor, la nota interna ni a qué reporte apunta un duplicado. Sin `FEEDBACK_ENABLED`
todas las rutas responden 404. La revisión del master es de HU-FB-04 (otro router).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile
from pydantic import BaseModel, Field

from core.api.deps import Principal, es_owner_o_master, get_principal
from core.api.errors import ApiError
from core.config import get_settings
from core.feedback import catalogo
from core.feedback import reportes as svc


def _habilitado() -> None:
    if not get_settings().feedback_enabled:
        raise ApiError.not_found()


router = APIRouter(prefix="/feedback", tags=["feedback"], dependencies=[Depends(_habilitado)])

Tipo = Literal["error", "mejora", "idea"]
Estado = Literal["recibido", "en_revision", "aprobado", "rechazado"]


class FeedbackOpcion(BaseModel):
    clave: str
    etiqueta: str


class FeedbackLimites(BaseModel):
    max_adjuntos: int
    max_bytes_por_archivo: int
    max_bytes_total: int
    mimetypes: list[str]


class FeedbackCatalogo(BaseModel):
    tipos: list[FeedbackOpcion]
    areas: list[FeedbackOpcion] = Field(description="Secciones del panel más `otra`.")
    limites: FeedbackLimites


class FeedbackAdjunto(BaseModel):
    id: uuid.UUID
    mimetype: str
    bytes: int
    nombre_original: str | None
    url: str = Field(description="URL firmada de vida corta (10 minutos).")


class _FeedbackBase(BaseModel):
    id: uuid.UUID
    autor_admin_id: uuid.UUID
    tipo: Tipo
    area: str
    titulo: str
    descripcion: str
    estado: Estado
    motivo_codigo: str | None = Field(description="Por qué se rechazó (`duplicado`, `no_reproducible`, …).")
    motivo: str | None = Field(description="Explicación del revisor para el cliente.")
    creditos_otorgados: int | None = Field(description="Créditos de regalo, si se aprobó.")
    created_at: datetime
    decidido_en: datetime | None


class FeedbackReporte(_FeedbackBase):
    adjuntos: list[FeedbackAdjunto]


class FeedbackReporteResumen(_FeedbackBase):
    adjuntos: int = Field(description="Cuántos adjuntos tiene.")


class FeedbackPagina(BaseModel):
    items: list[FeedbackReporteResumen]
    next_cursor: str | None


_ERRORES_ENVIO: dict[int | str, dict[str, Any]] = {
    200: {"description": "Replay: la `Idempotency-Key` ya se usó; devuelve el reporte original.", "model": FeedbackReporte},
    400: {"description": "`idempotency_key_requerida` o `idempotency_key_invalida`."},
    404: {"description": "Reportes apagados (`FEEDBACK_ENABLED`)."},
    409: {"description": "`idempotency_key_en_uso`: la clave es de un reporte de otro usuario."},
    413: {"description": "`adjunto_muy_grande` (8 MB por archivo) o `adjuntos_muy_grandes` (25 MB en total)."},
    422: {
        "description": "`adjunto_invalido` (tipo o firma), `demasiados_adjuntos` (más de 5), `adjunto_repetido`, "
        "`tipo_invalido`, `area_invalida`, `titulo_invalido`, `descripcion_invalida` o `validation_error`."
    },
    429: {"description": "`demasiados_reportes` o `demasiados_reportes_del_negocio`, con `Retry-After`."},
    503: {"description": "`almacenamiento_no_disponible`, con `Retry-After`; no se guardó nada."},
}


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.not_found()
    return principal.tenant_id


def _error(exc: svc.ErrorFeedback) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, details=exc.details, headers=exc.headers)


@router.get("/catalogo", response_model=FeedbackCatalogo)
async def get_catalogo(principal: Annotated[Principal, Depends(get_principal)]) -> FeedbackCatalogo:
    """Tipos, áreas y límites del formulario de reporte."""
    return FeedbackCatalogo(
        tipos=[FeedbackOpcion(clave=k, etiqueta=v) for k, v in catalogo.TIPOS.items()],
        areas=[FeedbackOpcion(clave=k, etiqueta=v) for k, v in catalogo.areas().items()],
        limites=FeedbackLimites(
            max_adjuntos=catalogo.MAX_ADJUNTOS,
            max_bytes_por_archivo=catalogo.MAX_BYTES_POR_ARCHIVO,
            max_bytes_total=catalogo.MAX_BYTES_TOTAL,
            mimetypes=list(catalogo.MIMETYPES),
        ),
    )


@router.post("/reports", status_code=201, response_model=FeedbackReporte, responses=_ERRORES_ENVIO)
async def post_report(
    response: Response,
    principal: Annotated[Principal, Depends(get_principal)],
    tipo: Annotated[str, Form(description="`error`, `mejora` o `idea`.")],
    area: Annotated[str, Form(max_length=80, description="Clave de un área de `GET /feedback/catalogo`.")],
    titulo: Annotated[str, Form(min_length=3, max_length=120)],
    descripcion: Annotated[str, Form(min_length=10, max_length=2000)],
    adjuntos: Annotated[list[UploadFile] | None, File(description="Hasta 5: imágenes o PDF.")] = None,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", description="Obligatoria: una por formulario abierto.")
    ] = None,
) -> FeedbackReporte:
    """Envía un reporte de error, mejora o idea con evidencia. Idempotente por `Idempotency-Key`."""
    tenant_id = _tenant(principal)
    archivos = adjuntos or []
    try:
        svc.validar_clave(idempotency_key)
        if len(archivos) > catalogo.MAX_ADJUNTOS:
            # Antes de leer nada: no vale la pena cargar archivos que se van a rechazar.
            raise svc.demasiados_adjuntos()
        entradas = [
            svc.AdjuntoEntrada(
                nombre=archivo.filename,
                mimetype=archivo.content_type,
                contenido=await archivo.read(catalogo.MAX_BYTES_POR_ARCHIVO + 1),
            )
            for archivo in archivos
        ]
        reporte, creado = await svc.enviar_reporte(
            tenant_id=tenant_id,
            autor_admin_id=principal.admin.id,
            idempotency_key=idempotency_key,
            tipo=tipo,
            area=area,
            titulo=titulo,
            descripcion=descripcion,
            adjuntos=entradas,
        )
    except svc.ErrorFeedback as exc:
        raise _error(exc) from exc
    if not creado:
        response.status_code = 200
    return FeedbackReporte.model_validate(reporte)


@router.get("/reports", response_model=FeedbackPagina)
async def list_reports(
    principal: Annotated[Principal, Depends(get_principal)],
    alcance: Annotated[Literal["mios", "negocio"], Query(description="`negocio` solo para el propietario.")] = "mios",
    estado: Annotated[Estado | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> FeedbackPagina:
    """Mis reportes, del más nuevo al más viejo; el propietario puede ver todos los del negocio."""
    try:
        pagina = await svc.listar_reportes(
            tenant_id=_tenant(principal),
            admin_id=principal.admin.id,
            puede_ver_negocio=es_owner_o_master(principal),
            alcance=alcance,
            estado=estado,
            cursor=cursor,
            limit=limit,
        )
    except svc.ErrorFeedback as exc:
        raise _error(exc) from exc
    return FeedbackPagina.model_validate(pagina)


@router.get("/reports/{report_id}", response_model=FeedbackReporte)
async def get_report(
    report_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> FeedbackReporte:
    """Detalle de un reporte con los enlaces firmados de su evidencia."""
    try:
        reporte = await svc.ver_reporte(
            tenant_id=_tenant(principal),
            admin_id=principal.admin.id,
            puede_ver_negocio=es_owner_o_master(principal),
            report_id=report_id,
        )
    except svc.ErrorFeedback as exc:
        raise _error(exc) from exc
    return FeedbackReporte.model_validate(reporte)

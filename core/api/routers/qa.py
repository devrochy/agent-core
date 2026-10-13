"""Centro de pruebas: `/api/v1/admin/qa/*` (SPEC-30, HU-QA-01/02, ADR-0133).

Sección RBAC `qa`, global (no se hereda del rol en un negocio): la tienen el master
y, en test, el rol tester. Lectura con GET; el resto exige escritura. Importar el
catálogo es solo del master. Cada tester escribe únicamente sobre su propia
ejecución del ciclo abierto.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.qa import service
from core.qa.catalogo import CatalogoInvalido
from core.qa.evidencias import MAX_BYTES
from core.qa.informe import informe_markdown

router = APIRouter(prefix="/admin/qa", tags=["admin · centro de pruebas"])

_leer = require("qa")
_escribir = require("qa", write=True)


def _error(exc: service.QaError) -> ApiError:
    if exc.tipo == "no_encontrado":
        return ApiError.not_found(exc.mensaje)
    if exc.tipo == "conflicto":
        return ApiError(409, "conflict", exc.mensaje)
    return ApiError(422, "invalid", exc.mensaje)


# ------------------------------------------------------------------ esquemas
class QaPaso(BaseModel):
    accion: str
    esperado: str | None = None


class QaProgreso(BaseModel):
    total: int
    terminadas: int
    sin_correr: int
    en_curso: int
    omitida: int
    ok: int
    bloqueada: int
    falla: int


class QaItem(BaseModel):
    slug: str
    seccion: Literal["pruebas", "test", "prod"]
    orden: int
    modulo: str | None = None
    grupo: str
    titulo: str
    resumen: str | None = None
    contexto: str | None = None
    prioridad: str | None = None
    plataforma: list[str] = []
    precondiciones: list[str] = []
    datos: list[str] = []
    pasos: list[QaPaso] = []
    esperado: str | None = None
    porque: str | None = None
    como: str | None = None
    vars: list[str] = []
    quien: str | None = None
    hu: list[str] = []
    origen: list[str] = []
    estado: str = Field(description="Pruebas: estado consolidado del ciclo. Test/prod: estado de la configuración.")
    ejecuciones: dict[str, int] = Field(default_factory=dict, description="Pruebas: cuántas ejecuciones hay en cada estado.")
    nota: str | None = None
    actualizado_en: str | None = None


class QaCicloRef(BaseModel):
    id: uuid.UUID
    nombre: str
    abierto_en: str | None = None


class QaCatalogoRef(BaseModel):
    version: int
    fecha: str
    commit_docs: str | None = None


class QaCatalogo(BaseModel):
    ciclo: QaCicloRef | None
    catalogo: QaCatalogoRef | None
    progreso: QaProgreso
    items: list[QaItem]


class QaPasoResultado(BaseModel):
    estado: str | None = None
    nota: str | None = None


class QaAdjunto(BaseModel):
    id: uuid.UUID
    paso: int
    nombre: str
    mimetype: str
    tamano: int
    subido_en: str | None = None


class QaResultadoPrueba(BaseModel):
    estado: str
    nota: str | None = None
    pasos: dict[str, QaPasoResultado] = Field(default_factory=dict, description="Por número de paso (desde 1).")
    adjuntos: list[QaAdjunto] = []


class QaTester(BaseModel):
    id: uuid.UUID
    nombre: str
    email: str


class QaEjecucion(BaseModel):
    id: uuid.UUID
    cycle_id: uuid.UUID
    ciclo_nombre: str
    tester: QaTester
    estado: Literal["en_curso", "enviada"]
    editable: bool
    comentario: str | None = None
    iniciada_en: str | None = None
    actualizada_en: str | None = None
    enviada_en: str | None = None
    progreso: QaProgreso
    resultados: dict[str, QaResultadoPrueba] | None = Field(default=None, description="Por slug de prueba.")
    informe: dict[str, Any] | None = Field(default=None, description="Informe congelado al enviar (formato 1).")


class QaPasoIn(BaseModel):
    estado: Literal["ok", "falla", "bloqueado", "omitido"] | None = Field(
        default=None, description="`null` desmarca el paso."
    )
    nota: str | None = Field(default=None, description="Si se envía (aunque sea `null`), reemplaza la nota.")


class QaNotaIn(BaseModel):
    nota: str | None = None


class QaEnviarIn(BaseModel):
    comentario: str | None = None


class QaCerrarCicloIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=120, description="Nombre del ciclo que se abre.")


class QaCerrarCiclo(BaseModel):
    cerrado_id: uuid.UUID
    abierto_id: uuid.UUID
    nombre: str


class QaCicloEjecucion(BaseModel):
    run_id: uuid.UUID
    tester: str
    estado: str
    enviada_en: str | None = None
    progreso: QaProgreso


class QaCiclo(BaseModel):
    id: uuid.UUID
    nombre: str
    abierto_en: str | None = None
    cerrado_en: str | None = None
    abierto: bool
    catalogo: QaCatalogoRef
    progreso: QaProgreso
    por_prueba: dict[str, str] = Field(default_factory=dict, description="Estado consolidado por slug.")
    ejecuciones: list[QaCicloEjecucion] = []


class QaConfigIn(BaseModel):
    estado: Literal["pendiente", "en_curso", "listo", "no_aplica"] | None = None
    nota: str | None = Field(default=None, description="Si se envía (aunque sea `null`), reemplaza la nota.")


class QaConfig(BaseModel):
    slug: str
    estado: str
    nota: str | None = None
    actualizado_en: str | None = None


class QaImportIn(BaseModel):
    contenido: str = Field(description="El YAML completo de `11-pruebas/catalogo.yaml`.", max_length=5_000_000)
    commit_docs: str | None = Field(default=None, description="Commit de solvershub-docs del que sale.")


class QaImport(BaseModel):
    version_id: uuid.UUID
    version: int
    items: int
    creada: bool
    ciclo_id: uuid.UUID
    ciclo_nombre: str


class QaEvento(BaseModel):
    campo: str
    paso: int | None = None
    antes: str | None = None
    despues: str | None = None
    por: str
    en: str | None = None
    run_id: uuid.UUID | None = None


# ------------------------------------------------------------------ catálogo
@router.get("/catalog", response_model=QaCatalogo)
async def get_catalog(
    principal: Annotated[Principal, Depends(_leer)],
    seccion: Annotated[Literal["pruebas", "test", "prod"] | None, Query()] = None,
) -> dict[str, Any]:
    """Catálogo del ciclo abierto con el estado consolidado de cada prueba y el de la config."""
    return await service.catalogo_con_estado(seccion)


@router.post("/catalog/import", response_model=QaImport)
async def import_catalog(
    payload: QaImportIn,
    principal: Annotated[Principal, Depends(_escribir)],
) -> QaImport:
    """Importa el catálogo (solo master). Idempotente: el mismo contenido no se duplica."""
    if principal.admin.role != "master":
        raise ApiError.forbidden("Solo el admin de plataforma importa el catálogo.")
    try:
        res = await service.importar_catalogo(
            payload.contenido, admin_id=principal.admin.id, commit_docs=payload.commit_docs
        )
    except CatalogoInvalido as exc:
        raise ApiError(422, "invalid_catalog", "Catálogo inválido: " + "; ".join(exc.errores[:20])) from exc
    return QaImport(
        version_id=res.version_id,
        version=res.version,
        items=res.items,
        creada=res.creada,
        ciclo_id=res.ciclo_id,
        ciclo_nombre=res.ciclo_nombre,
    )


@router.get("/items/{slug}/history", response_model=list[QaEvento])
async def item_history(
    slug: str,
    principal: Annotated[Principal, Depends(_leer)],
    cycle_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
    """Historial de una prueba en el ciclo (abierto por defecto) o de un punto de configuración."""
    return await service.historial(principal.admin, slug, cycle_id)


# ------------------------------------------------------------------ mi ejecución
@router.get("/runs/me", response_model=QaEjecucion | None)
async def get_my_run(principal: Annotated[Principal, Depends(_leer)]) -> dict[str, Any] | None:
    """Mi ejecución del ciclo abierto (`null` si todavía no la empecé)."""
    return await service.mi_ejecucion(principal.admin)


@router.post("/runs/me/start", response_model=QaEjecucion)
async def start_my_run(principal: Annotated[Principal, Depends(_escribir)]) -> dict[str, Any]:
    """Empieza (o devuelve) mi ejecución del ciclo abierto."""
    try:
        return await service.iniciar_ejecucion(principal.admin)
    except service.QaError as exc:
        raise _error(exc) from exc


@router.put("/runs/me/items/{slug}/steps/{paso}", response_model=QaEjecucion)
async def mark_step(
    slug: str,
    paso: int,
    payload: QaPasoIn,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Marca un paso de mi ejecución (guardado inmediato) y recalcula la prueba."""
    try:
        return await service.marcar_paso(
            principal.admin,
            slug,
            paso,
            estado=payload.estado,
            nota=payload.nota,
            cambiar_nota="nota" in payload.model_fields_set,
        )
    except service.QaError as exc:
        raise _error(exc) from exc


@router.put("/runs/me/items/{slug}", response_model=QaEjecucion)
async def note_item(
    slug: str,
    payload: QaNotaIn,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Nota general de una prueba en mi ejecución."""
    try:
        return await service.nota_de_prueba(principal.admin, slug, payload.nota)
    except service.QaError as exc:
        raise _error(exc) from exc


@router.post("/runs/me/attachments", response_model=QaAdjunto, status_code=201)
async def upload_attachment(
    principal: Annotated[Principal, Depends(_escribir)],
    item_slug: Annotated[str, Form()],
    paso: Annotated[int, Form()],
    archivo: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Adjunta una captura (jpeg, png, webp o pdf; hasta 5 MB) a un paso de mi ejecución."""
    contenido = await archivo.read(MAX_BYTES + 1)
    try:
        return await service.subir_adjunto(
            principal.admin,
            item_slug,
            paso,
            nombre_original=archivo.filename,
            mimetype=archivo.content_type,
            contenido=contenido,
        )
    except service.QaError as exc:
        raise _error(exc) from exc


@router.delete("/attachments/{adjunto_id}", status_code=204)
async def delete_attachment(
    adjunto_id: uuid.UUID,
    principal: Annotated[Principal, Depends(_escribir)],
) -> None:
    """Quita una captura de mi ejecución en curso."""
    try:
        await service.borrar_adjunto(principal.admin, adjunto_id)
    except service.QaError as exc:
        raise _error(exc) from exc


@router.get("/attachments/{adjunto_id}", response_class=FileResponse)
async def get_attachment(
    adjunto_id: uuid.UUID,
    principal: Annotated[Principal, Depends(_leer)],
) -> FileResponse:
    """Sirve una captura (dueño de la ejecución o master)."""
    try:
        adj, ruta = await service.adjunto_para_servir(principal.admin, adjunto_id)
    except service.QaError as exc:
        raise _error(exc) from exc
    return FileResponse(
        ruta,
        media_type=adj.mimetype,
        filename=adj.nombre_original or f"captura-{adj.id}",
        content_disposition_type="inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.post("/runs/me/submit", response_model=QaEjecucion)
async def submit_my_run(
    payload: QaEnviarIn,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Envía mi informe: la ejecución queda en solo lectura y se avisa por correo al dueño."""
    try:
        return await service.enviar_ejecucion(principal.admin, payload.comentario)
    except service.QaError as exc:
        raise _error(exc) from exc


# ------------------------------------------------------------------ informes
@router.get("/runs", response_model=list[QaEjecucion])
async def list_runs(
    principal: Annotated[Principal, Depends(_leer)],
    cycle_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
    """Ejecuciones e informes: el master ve todos, el resto solo los suyos."""
    return await service.listar_ejecuciones(principal.admin, cycle_id)


@router.get("/runs/{run_id}", response_model=QaEjecucion)
async def get_run(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(_leer)],
) -> dict[str, Any]:
    try:
        return await service.obtener_ejecucion(principal.admin, run_id)
    except service.QaError as exc:
        raise _error(exc) from exc


@router.get("/runs/{run_id}/report.md", response_class=PlainTextResponse)
async def get_run_markdown(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(_leer)],
) -> PlainTextResponse:
    """Informe enviado en Markdown, para descargar."""
    try:
        run = await service.obtener_ejecucion(principal.admin, run_id)
    except service.QaError as exc:
        raise _error(exc) from exc
    if not run.get("informe"):
        raise ApiError(409, "conflict", "La ejecución todavía no tiene informe: falta enviarla.")
    texto = informe_markdown(run["informe"], enlace_adjunto="/api/qa/adjuntos/")
    return PlainTextResponse(
        texto,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="informe-pruebas-{run_id}.md"'},
    )


# ------------------------------------------------------------------ ciclos y configuración
@router.get("/cycles", response_model=list[QaCiclo])
async def list_cycles(principal: Annotated[Principal, Depends(_leer)]) -> list[dict[str, Any]]:
    """Ciclos con su progreso consolidado; los cerrados, con la foto guardada al cerrarlos."""
    return await service.listar_ciclos()


@router.post("/cycles/close", response_model=QaCerrarCiclo)
async def close_cycle(
    payload: QaCerrarCicloIn,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Cierra el ciclo abierto (foto fija) y abre uno nuevo con la última versión del catálogo.

    Congela las ejecuciones en curso de todos: solo el admin de plataforma."""
    if principal.admin.role != "master":
        raise ApiError.forbidden("Solo el admin de plataforma cierra un ciclo.")
    try:
        return await service.cerrar_ciclo(principal.admin, payload.nombre)
    except service.QaError as exc:
        raise _error(exc) from exc


@router.put("/config/{slug}", response_model=QaConfig)
async def update_config(
    slug: str,
    payload: QaConfigIn,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Estado y nota de un punto de configuración de test o de paso a producción."""
    try:
        return await service.actualizar_config(
            principal.admin,
            slug,
            estado=payload.estado,
            nota=payload.nota,
            cambiar_nota="nota" in payload.model_fields_set,
        )
    except service.QaError as exc:
        raise _error(exc) from exc

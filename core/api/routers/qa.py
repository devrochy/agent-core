"""Centro de pruebas: `/api/v1/admin/qa/*` (SPEC-30, HU-QA-01/02, ADR-0133).

Sección RBAC `qa`, global (no se hereda del rol en un negocio): la tienen el master
y, en test, el rol tester. Lectura con GET; el resto exige escritura. Importar el
catálogo y gestionar testers es solo del master; cerrar el ciclo, del master o un
tester (ADR-0134). Cada tester escribe únicamente sobre su propia ejecución del
ciclo abierto.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from core.admin.roles import actua_como_master
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
    if exc.tipo == "prohibido":
        return ApiError.forbidden(exc.mensaje)
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
    seccion: Literal["pruebas", "test", "prod", "datos"]
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
    entorno: Literal["panel", "apk", "whatsapp", "web", "api"] | None = Field(
        default=None, description="Pruebas: dónde se ejecuta (HU-QA-08)."
    )
    area: str | None = Field(default=None, description="Pruebas: sección del panel o pantalla de la APK.")
    subseccion: str | None = Field(
        default=None, description="Pruebas: tercer nivel dentro del área (catálogo v7, HU-QA-08)."
    )
    requiere: list[str] = Field(default_factory=list, description="Pruebas: slugs de la guía de datos que necesita.")
    estado: str = Field(
        description="Pruebas: estado consolidado del ciclo. Test/prod: estado de la configuración. Datos: `guia`."
    )
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


class QaCuentaSeccion(BaseModel):
    """Conteo de estados de una sección, área o subsección (HU-QA-08)."""

    total: int
    ok: int
    falla: int
    bloqueada: int
    omitida: int
    en_curso: int
    sin_correr: int


class QaSubseccionEvo(QaCuentaSeccion):
    subseccion: str


class QaAreaEvo(QaCuentaSeccion):
    area: str
    subsecciones: list[QaSubseccionEvo]


class QaSeccionEvo(QaCuentaSeccion):
    entorno: str
    areas: list[QaAreaEvo]


class QaVersionEvo(BaseModel):
    version: int
    fecha: str
    commit_docs: str | None = None
    importada_en: str | None = None
    total_pruebas: int
    ciclo: dict[str, Any] | None = Field(default=None, description="El ciclo más reciente que usó esta versión.")
    secciones: list[QaSeccionEvo]


class QaEvolucion(BaseModel):
    versiones: list[QaVersionEvo]


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
    seccion: Annotated[Literal["pruebas", "test", "prod", "datos"] | None, Query()] = None,
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
    texto = informe_markdown(run["informe"], enlace_adjunto="/api/qa/attachments/")
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


@router.get("/evolution", response_model=QaEvolucion)
async def evolution(principal: Annotated[Principal, Depends(_leer)]) -> dict[str, Any]:
    """Línea de tiempo de versiones del catálogo con el resultado por entorno, área y subsección."""
    return await service.evolucion()


@router.post("/cycles/close", response_model=QaCerrarCiclo)
async def close_cycle(
    payload: QaCerrarCicloIn,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Cierra el ciclo abierto (foto fija) y abre uno nuevo con la última versión del catálogo.

    Congela las ejecuciones en curso de todos: master, o tester en test (ADR-0134)."""
    if not actua_como_master(principal.admin):
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


# ------------------------------------------------------------------ testers (HU-QA-04, ADR-0134)
class QaTesterOut(BaseModel):
    id: uuid.UUID
    email: str
    estado: Literal["invitado", "activo", "vencido", "revocado"]
    creado_en: str | None = None
    invitado_en: str | None = None
    expira_en: str | None = None
    aceptado_en: str | None = None


class QaTestersOut(BaseModel):
    habilitado: bool = Field(description="`QA_TESTERS_ENABLED` en este entorno.")
    testers: list[QaTesterOut]


class QaInvitarTesterIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class QaTesterInvitado(BaseModel):
    tester: QaTesterOut
    correo_enviado: bool
    enlace: str | None = Field(
        default=None, description="Enlace de un solo uso, solo si el correo no salió (SMTP de sistema sin configurar)."
    )


def _solo_master(principal: Principal) -> None:
    # Gestionar cuentas es solo del master: un tester no invita ni revoca testers.
    if principal.admin.role != "master":
        raise ApiError.forbidden("Solo el admin de plataforma gestiona testers.")


@router.get("/testers", response_model=QaTestersOut)
async def list_testers(principal: Annotated[Principal, Depends(_leer)]) -> dict[str, Any]:
    from core.admin.roles import testers_habilitados
    from core.qa import testers

    _solo_master(principal)
    return {"habilitado": testers_habilitados(), "testers": await testers.listar_testers()}


@router.post("/testers", response_model=QaTesterInvitado, status_code=201)
async def invite_tester(
    payload: QaInvitarTesterIn,
    response: Response,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Invita (o reinvita) a un tester por correo con un enlace de un solo uso."""
    from core.qa import testers

    _solo_master(principal)
    try:
        tester, url, enviado = await testers.invitar_tester(principal.admin, payload.email)
    except service.QaError as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    # El enlace solo vuelve si el correo no salió (sin SMTP de sistema): el master lo comparte a mano.
    return {"tester": tester, "enlace": None if enviado else url, "correo_enviado": enviado}


@router.post("/testers/{tester_id}/revoke", response_model=QaTesterOut)
async def revoke_tester(
    tester_id: uuid.UUID,
    principal: Annotated[Principal, Depends(_escribir)],
) -> dict[str, Any]:
    """Desactiva al tester y cierra sus sesiones: el siguiente pedido ya falla."""
    from core.qa import testers

    _solo_master(principal)
    try:
        return await testers.revocar_tester(tester_id)
    except service.QaError as exc:
        raise _error(exc) from exc


# ------------------------------------------------------------------ datos de prueba (HU-QA-07)
class QaDatosCuenta(BaseModel):
    rol: Literal["dueno", "gestor", "colaborador"]
    titulo: str
    uso: str
    email: str
    creada: bool


class QaDatosContenido(BaseModel):
    habilidad: str
    detalle: list[str]


class QaDatosNegocio(BaseModel):
    id: uuid.UUID
    slug: str
    nombre: str | None = None


class QaDatosChatWeb(BaseModel):
    clave: str = Field(description="Clave pública del widget (`/w/<clave>`).")
    enlace: str | None = Field(default=None, description="`LANDING_BASE_URL/w/<clave>`, para conversar como cliente.")
    activo: bool


class QaDatosAgente(BaseModel):
    key: str
    nombre: str
    rol: str = Field(description="Coordinador, Especialista o Backoffice.")
    por_defecto: bool


class QaDatosAgentOS(BaseModel):
    """Agent OS del negocio de prueba (HU-QA-09): v3 con procedimientos, agentes del preset v2
    en el chat web y enrutamiento activo."""

    estado: Literal["listo", "pendiente", "error"]
    motivo: str | None = Field(default=None, description="Por qué no quedó listo; se reintenta con «Preparar mis datos».")
    chat_web: QaDatosChatWeb | None = None
    agentes: list[QaDatosAgente] = Field(default_factory=list, description="Agentes con instancia en el chat web.")


class QaDatos(BaseModel):
    estado: Literal["creando", "listo", "error", "purgado"]
    error: str | None = None
    agent_os: QaDatosAgentOS | None = Field(default=None, description="Solo con los datos listos y el negocio vivo.")
    negocio: QaDatosNegocio
    cuentas: list[QaDatosCuenta]
    contenido: list[QaDatosContenido]
    creado_en: str | None = None
    actualizado_en: str | None = None
    contrasenas: dict[str, str] | None = Field(
        default=None,
        description="Solo al preparar o al pedir contraseñas nuevas: se muestran una vez y no se guardan.",
    )


class QaMisDatos(BaseModel):
    disponible: bool = Field(description="`QA_TESTERS_ENABLED` en este entorno: los datos solo existen en test.")
    datos: QaDatos | None
    contenido: list[QaDatosContenido]


def _solo_plataforma(principal: Principal) -> None:
    # Un colaborador con permiso explícito de `qa` no se crea negocios premium propios.
    if not actua_como_master(principal.admin):
        raise ApiError.forbidden("Los datos de prueba son del admin de plataforma y de los testers.")


@router.get("/datos", response_model=QaMisDatos)
async def get_mis_datos(principal: Annotated[Principal, Depends(_leer)]) -> dict[str, Any]:
    """Negocio de prueba y cuentas del tester (sin contraseñas)."""
    from core.qa import datos

    _solo_plataforma(principal)
    return await datos.mis_datos(principal.admin)


@router.post(
    "/datos",
    response_model=QaDatos,
    status_code=201,
    responses={200: {"model": QaDatos, "description": "Ya estaba preparado: solo se puso al día el Agent OS."}},
)
async def preparar_mis_datos(
    response: Response, principal: Annotated[Principal, Depends(_escribir)]
) -> dict[str, Any]:
    """Prepara el negocio de prueba del tester con sus datos y cuentas (201, con las contraseñas)
    y lo deja listo para el Agent OS. Si ya está preparado, solo pone al día el Agent OS: 200 sin
    contraseñas (HU-QA-09). 409 si hay otra preparación en curso."""
    from core.qa import datos

    _solo_plataforma(principal)
    try:
        salida = await datos.preparar(principal.admin)
    except service.QaError as exc:
        raise _error(exc) from exc
    if salida.get("contrasenas") is None:
        response.status_code = 200
    response.headers["Cache-Control"] = "no-store"
    return salida


@router.post("/datos/contrasenas", response_model=QaDatos)
async def contrasenas_nuevas(
    response: Response, principal: Annotated[Principal, Depends(_escribir)]
) -> dict[str, Any]:
    """Contraseñas nuevas para las tres cuentas de prueba; cierra sus sesiones."""
    from core.qa import datos

    _solo_plataforma(principal)
    try:
        salida = await datos.contrasenas_nuevas(principal.admin)
    except service.QaError as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return salida

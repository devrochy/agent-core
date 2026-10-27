"""Primeros pasos del negocio: `GET /api/v1/admin/onboarding` (HU-SH-06, ADR-0124).

El checklist del Resumen calculado del estado real (`core/primeros_pasos`): datos del negocio,
WhatsApp conectado y verificado, catálogo o servicios, probar el asistente, equipo invitado, el
tipo de negocio que espera un plan más alto y las habilidades pendientes de configurar. Cada paso
dice si el usuario puede actuar sobre él (sección del RBAC con escritura): el panel no muestra
una acción que daría 403.

Al evaluar se registra la primera vez de cada paso cumplido (`onboarding_hitos`, el embudo de
activación). Es un efecto secundario idempotente y no falla la lectura.

Requiere la sección `dashboard` (la misma del Resumen).

**Recorrido de primer uso (HU-SH-10, adenda a ADR-0124).** Aparte de los pasos, el negocio guarda
dónde va la persona en el asistente de primer uso (`tenants.settings.onboarding`, ver
`core/primeros_pasos/recorrido.py`): `avanzar`, `omitir`, `reanudar` y `finalizar`. Los pasos
siguen calculándose del estado real; `finalizar` lo valida el servidor y responde 409
`faltan_pasos` si falta algo. Solo el propietario del negocio (o un admin de plataforma) lo mueve.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.agents import editor as editor_svc
from core.agents import versiones
from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_error, plan_limit_responses
from core.billing.enforcement import QuotaExceeded
from core.db.models import utcnow
from core.db.session import get_session
from core.primeros_pasos import recorrido as rec
from core.primeros_pasos.hitos import evaluar
from core.primeros_pasos.reglas import DATOS_NEGOCIO, Estado, Motivo, Paso
from core.releases import promocion

router = APIRouter(prefix="/admin/onboarding", tags=["admin · primeros pasos"])


class PasoOut(BaseModel):
    clave: str = Field(
        description=(
            "datos_negocio | whatsapp | catalogo | probar_asistente | equipo | activar_preset | "
            "configurar_<habilidad>"
        )
    )
    estado: Estado
    motivo: Motivo | None = Field(
        default=None, description="Por qué no está completo (clave para el panel)."
    )
    cantidad: int | None = Field(default=None, description="Elementos cargados (paso `catalogo`).")
    pack: str | None = Field(
        default=None, description="Habilidad del paso (`catalogo` y `configurar_*`)."
    )
    tipo: Literal["catalogo", "servicios", "cartera"] | None = None
    titulo: str | None = Field(
        default=None, description="Nombre visible de la habilidad o del tipo de negocio."
    )
    explicacion: str | None = Field(
        default=None, description="Guía de configuración de la habilidad (su catálogo)."
    )
    plan_requerido: str | None = Field(
        default=None, description="Plan que necesita el tipo de negocio."
    )
    puede_actuar: bool = Field(description="El usuario tiene escritura en la sección del paso.")
    completado_en: datetime | None = Field(
        default=None, description="Primera vez que se cumplió (hito)."
    )
    segundos_desde_alta: int | None = Field(
        default=None, description="Del alta a la primera vez que se cumplió."
    )


class RecorridoOut(BaseModel):
    estado: rec.EstadoDelRecorrido = Field(
        description="pendiente | en_curso | omitido | completo | no_aplica (negocio anterior al corte o función apagada)."
    )
    paso: int = Field(description="Paso del asistente donde va (1 a `total_pasos`).")
    total_pasos: int
    aplica: bool = Field(
        description="El recorrido es del negocio (si no, el panel no muestra nada)."
    )
    abrir: bool = Field(
        description="El panel debe abrir el asistente solo: nunca se tocó y quien consulta es propietario del negocio."
    )
    avisar: bool = Field(
        description="Aviso persistente: empezó o saltó el recorrido y no lo terminó."
    )
    omitido_en: datetime | None = None
    completado_en: datetime | None = None
    agente_id: str | None = Field(default=None, description="Agente que creó el asistente.")
    conexion_id: str | None = Field(
        default=None, description="Conexión de pruebas donde quedó publicado."
    )
    probado_en: datetime | None = Field(default=None, description="Primera prueba con ese agente.")


class PrimerosPasosResponse(BaseModel):
    alta_en: datetime
    origen: Literal["aprovisionamiento", "manual"]
    completos: int
    total: int
    pasos: list[PasoOut]
    recorrido: RecorridoOut


class AvanzarIn(BaseModel):
    paso: int = Field(ge=1, le=rec.TOTAL_PASOS, description="Paso al que llega la persona.")


def _puede_ver(principal: Principal, seccion: str | None) -> bool:
    """¿Puede leer la sección del paso? Si no, el paso no le muestra cuánto hay cargado."""
    if seccion is None:
        return False
    return principal.admin.role == "master" or principal.permissions.get(seccion) in (
        "read",
        "write",
    )


def _puede_actuar(principal: Principal, seccion: str | None) -> bool:
    if seccion is None:
        return False
    if principal.admin.role == "master":
        return True
    return principal.permissions.get(seccion) == "write"


@router.get("", response_model=PrimerosPasosResponse)
async def get_primeros_pasos(
    principal: Annotated[Principal, Depends(require("dashboard"))],
) -> PrimerosPasosResponse:
    if principal.tenant_id is None:
        raise ApiError.bad_request("No hay negocio activo.", "no_tenant")
    resultado = await evaluar(principal.tenant_id)
    if resultado is None:
        raise ApiError.not_found("Negocio no encontrado.")
    evaluacion, hitos = resultado

    def _out(paso: Paso) -> PasoOut:
        hito = hitos.get(paso.clave)
        return PasoOut(
            clave=paso.clave,
            estado=paso.estado,
            motivo=paso.motivo,
            cantidad=paso.cantidad if _puede_ver(principal, paso.seccion) else None,
            pack=paso.pack,
            tipo=paso.tipo,  # type: ignore[arg-type]
            titulo=paso.titulo,
            explicacion=paso.explicacion,
            plan_requerido=paso.plan_requerido,
            puede_actuar=_puede_actuar(principal, paso.seccion),
            completado_en=hito.completado_en if hito else None,
            segundos_desde_alta=hito.segundos_desde_alta if hito else None,
        )

    pasos = [_out(p) for p in evaluacion.pasos]
    return PrimerosPasosResponse(
        alta_en=evaluacion.alta_en,
        origen=evaluacion.origen,
        completos=sum(1 for p in pasos if p.estado == "completo"),
        total=len(pasos),
        pasos=pasos,
        recorrido=_recorrido_out(await _leer_recorrido(principal), principal),
    )


# --------------------------------------------------------------------- recorrido de primer uso


def _es_propietario(principal: Principal) -> bool:
    """Propietario del negocio activo que no es admin de plataforma (ni master ni tester)."""
    if principal.admin.role in ("master", "tester"):
        return False
    return any(
        role == "owner" for (tid, _name, role) in principal.tenants if tid == principal.tenant_id
    )


def _recorrido_out(actual: rec.Recorrido, principal: Principal) -> RecorridoOut:
    # Solo el propietario es redirigido: master y tester cambian de negocio todo el día y no se
    # bloquean, aunque sí ven el aviso.
    return RecorridoOut(
        estado=actual.estado,
        paso=actual.paso,
        total_pasos=rec.TOTAL_PASOS,
        aplica=actual.aplica,
        abrir=actual.obliga and _es_propietario(principal),
        avisar=actual.avisa,
        omitido_en=actual.omitido_en,
        completado_en=actual.completado_en,
        agente_id=actual.agente_id,
        conexion_id=actual.conexion_id,
        probado_en=actual.probado_en,
    )


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("No hay negocio activo.", "no_tenant")
    return principal.tenant_id


async def _leer_recorrido(principal: Principal) -> rec.Recorrido:
    try:
        return await rec.leer(_tenant(principal))
    except rec.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None


def _actor(principal: Principal) -> str:
    return f"admin:{principal.admin.id}"


async def _mover(principal: Principal, accion: str, cambios: dict[str, Any]) -> RecorridoOut:
    try:
        nuevo = await rec.fijar(_tenant(principal), cambios, actor=_actor(principal), accion=accion)
    except rec.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return _recorrido_out(nuevo, principal)


def _transicion_invalida(exc: rec.TransicionInvalida) -> ApiError:
    return ApiError(409, "transicion_invalida", str(exc))


@router.post("/avanzar", response_model=RecorridoOut)
async def avanzar_recorrido(
    payload: AvanzarIn,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> RecorridoOut:
    """Guarda el paso del asistente donde va la persona (y retoma un recorrido omitido)."""
    require_owner_or_master(principal)
    actual = await _leer_recorrido(principal)
    try:
        cambios = rec.cambios_de_avanzar(actual, payload.paso)
    except rec.TransicionInvalida as exc:
        raise _transicion_invalida(exc) from None
    return await _mover(principal, "avanzar", cambios)


@router.post("/omitir", response_model=RecorridoOut)
async def omitir_recorrido(
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> RecorridoOut:
    """«Lo hago después»: el panel deja de abrir el asistente y muestra el aviso persistente."""
    require_owner_or_master(principal)
    actual = await _leer_recorrido(principal)
    try:
        cambios = rec.cambios_de_omitir(actual, por=str(principal.admin.id), ahora=utcnow())
    except rec.TransicionInvalida as exc:
        raise _transicion_invalida(exc) from None
    return await _mover(principal, "omitir", cambios)


@router.post("/reanudar", response_model=RecorridoOut)
async def reanudar_recorrido(
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> RecorridoOut:
    require_owner_or_master(principal)
    actual = await _leer_recorrido(principal)
    try:
        cambios = rec.cambios_de_reanudar(actual)
    except rec.TransicionInvalida as exc:
        raise _transicion_invalida(exc) from None
    return await _mover(principal, "reanudar", cambios)


@router.post("/finalizar", response_model=RecorridoOut)
async def finalizar_recorrido(
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> RecorridoOut:
    """Da por completo el recorrido. Lo valida el servidor con el estado real: 409 `faltan_pasos`
    (`details.faltan`) si no hay datos del negocio, agente publicado o una prueba con él."""
    require_owner_or_master(principal)
    actual = await _leer_recorrido(principal)
    resultado = await evaluar(_tenant(principal))
    if resultado is None:
        raise ApiError.not_found("Negocio no encontrado.")
    evaluacion, _hitos = resultado
    datos_completos = any(
        p.clave == DATOS_NEGOCIO and p.estado == "completo" for p in evaluacion.pasos
    )
    faltan = rec.claves_que_faltan(
        actual, datos_negocio_completo=datos_completos, prueba_hecha=False
    )
    try:
        cambios = rec.cambios_de_finalizar(actual, faltan, ahora=utcnow())
    except rec.TransicionInvalida as exc:
        raise _transicion_invalida(exc) from None
    except rec.FaltanPasos as exc:
        raise ApiError(
            409,
            "faltan_pasos",
            "Todavía faltan pasos para terminar.",
            details={"faltan": exc.faltan},
        ) from None
    return await _mover(principal, "finalizar", cambios)


# --------------------------------------------------------------------- dejar listo el agente


class DejarListoIn(BaseModel):
    agent_id: uuid.UUID = Field(description="Agente (borrador) que el asistente creó con `from-script`.")


class SubPasoOut(BaseModel):
    clave: Literal["conexion", "publicar", "activar"]
    estado: Literal["hecho", "ya_estaba", "pendiente_de_revision"]
    mensaje: str | None = None


class DejarListoOut(BaseModel):
    conexion_id: uuid.UUID = Field(description="Conexión de chat web (sin abrir al público) donde queda para probar.")
    release_id: uuid.UUID | None = None
    listo: bool = Field(description="El agente quedó activo y se puede probar con `POST /admin/playground/message`.")
    pasos: list[SubPasoOut]
    recorrido: RecorridoOut


async def _asegurar_conexion_de_pruebas(tid: uuid.UUID) -> tuple[uuid.UUID, bool]:
    """La conexión `webchat` del negocio; si no existe, la crea **cerrada** (`enabled=false`: nadie de
    fuera puede escribirle). 409 `canal_en_vivo` si ya atiende clientes: ahí el agente se publica
    desde Agentes, no desde el primer uso."""
    from core.gateway import chat_web_conexion

    async with get_session() as session:
        conexion = await chat_web_conexion.conexion_del_negocio(session, tid)
        creada = False
        if conexion is None:
            await chat_web_conexion.guardar_widget(session, tid, enabled=False, allowed_origins=[])
            conexion = await chat_web_conexion.conexion_del_negocio(session, tid)
            creada = True
        if conexion is None:  # no debería pasar: guardar_widget la acaba de crear
            raise ApiError(500, "conexion_no_creada", "No se pudo preparar el chat de pruebas.")
        if conexion.active and not creada:
            raise ApiError(
                409,
                "canal_en_vivo",
                "Tu chat web ya atiende clientes: publica el agente desde Agentes para no cambiarlo sin querer.",
            )
        return conexion.id, creada


@router.post("/dejar-listo", response_model=DejarListoOut, responses=plan_limit_responses("agentes"))
async def dejar_listo(
    payload: DejarListoIn,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> DejarListoOut:
    """Deja el agente del asistente listo para probar, de una vez y sin abrirlo a clientes: prepara un
    chat web cerrado, publica el agente ahí y activa su versión. Es idempotente y se puede
    reintentar. Si el gate de pass^k del negocio está cerrado, la activación queda
    `pendiente_de_revision` (no es un error: el agente queda publicado). Dueño o master."""
    from core.api.routers.agent_editor import _error, _ve_conexiones
    from core.api.routers.agents import _catalogo, _contrato_422, _puede_escribir

    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    await _ve_conexiones(principal, tid, write=True)
    actual = await _leer_recorrido(principal)
    if not actual.aplica or actual.estado == "completo":
        raise ApiError(409, "transicion_invalida", "El recorrido no admite dejar listo el agente.")
    actor = f"admin:{principal.admin.id}"
    pasos: list[SubPasoOut] = []

    conexion_id, creada = await _asegurar_conexion_de_pruebas(tid)
    pasos.append(SubPasoOut(clave="conexion", estado="hecho" if creada else "ya_estaba"))

    try:
        publicada = await editor_svc.publicar_en_conexiones(
            tid,
            payload.agent_id,
            [conexion_id],
            catalogo=_catalogo(),
            admin_id=principal.admin.id,
            actor=actor,
            lock_version=None,
            notes="Primer uso: agente listo para probar",
        )
    except versiones.AgenteNoEncontrado:
        raise ApiError.not_found("Agente no encontrado.") from None
    except editor_svc.ErrorDelEditor as exc:
        raise _error(exc) from None
    except versiones.ErroresDeContrato as exc:
        raise _contrato_422(exc.issues) from None
    except versiones.Conflicto as exc:
        raise ApiError(409, "conflict", str(exc)) from None
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from None
    resultado = next((c for c in publicada.connections if c.connection_id == conexion_id), None)
    if resultado is None or resultado.error or resultado.release_id is None:
        codigo = resultado.error if resultado and resultado.error else "publicacion_incompleta"
        raise ApiError(409, codigo, "No se pudo publicar el agente en el chat de pruebas.")
    pasos.append(SubPasoOut(clave="publicar", estado="hecho" if publicada.created else "ya_estaba"))

    release_id = resultado.release_id
    activo = False
    try:
        await promocion.promover(tid, conexion_id, release_id, actor=actor, motivo="onboarding")
        pasos.append(SubPasoOut(clave="activar", estado="hecho"))
        activo = True
    except promocion.ConflictoDeRelease as exc:
        if exc.codigo == "ya_activa":
            pasos.append(SubPasoOut(clave="activar", estado="ya_estaba"))
            activo = True
        elif exc.codigo == "gate_pendiente":
            pasos.append(SubPasoOut(clave="activar", estado="pendiente_de_revision", mensaje=str(exc)))
        else:
            raise ApiError(409, exc.codigo, str(exc), details=dict(exc.details) or None) from None

    cambios: dict[str, Any] = {"agente_id": str(payload.agent_id), "conexion_id": str(conexion_id)}
    if actual.agente_id != cambios["agente_id"]:
        cambios["probado_en"] = None  # otro agente: la prueba anterior no cuenta
    if actual.estado == "pendiente":
        cambios.update(estado="en_curso", paso=5)
    try:
        nuevo = await rec.fijar(tid, cambios, actor=actor, accion="dejar-listo")
    except rec.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return DejarListoOut(
        conexion_id=conexion_id,
        release_id=release_id,
        listo=activo,
        pasos=pasos,
        recorrido=_recorrido_out(nuevo, principal),
    )

"""Releases de una conexión: build, promoción y rollback en un clic (ADR-0117, ADR-0077, HU-AOS-14).

- `GET  /api/v1/admin/connections/{id}/releases` — releases de la conexión, de la más nueva a la
  más vieja (`limit`, `before` = número), con cuál es la activa y su último evento.
- `POST /api/v1/admin/connections/{id}/releases` — construye una candidata (no la activa). Con
  el mismo contenido que una existente devuelve esa (`created: false`).
- `GET  /api/v1/admin/connections/{id}/releases/{rid}` — detalle: el manifiesto **sin textos**
  (hashes y tamaños) y sus eventos.
- `POST /api/v1/admin/connections/{id}/releases/{rid}/promote` — la activa (promoción total;
  el canary llega con HU-AOS-32). Pasa por el gate de pass^k (HU-AOS-21): un veredicto
  rechazado contra la activa de ahora da 409 `gate_rechazado`; solo el master lo salta con
  `forzar` y `motivo_forzado`, que quedan en el evento y en la auditoría.
  Con la bandera del gate encendida (parte 4), una candidata sin veredicto da 409
  `gate_pendiente` y encola la medición (`details.estado`: la última medición; `details.encolado`).
- `POST /api/v1/admin/connections/{id}/releases/{rid}/evaluate` — pide medir la candidata contra la
  activa (parte 4): 202 si se encoló o ya está en curso; 200 si no hace falta medir.
- `GET  /api/v1/admin/connections/{id}/releases/{rid}/evaluation` — la medición contra la activa de
  ahora: veredicto por procedimiento, lo que quedó sin medir y el estado de la última corrida.
- `GET  /api/v1/admin/connections/{id}/releases/{rid}/diff?against=` — qué cambia frente a otra
  release de la conexión (por defecto la activa), agrupado por agentes, superpoderes y permisos, más
  lo que medirá el gate (HU-AOS-27). Sin textos.
- `POST /api/v1/admin/connections/{id}/releases/{rid}/stop` — el dueño detiene una candidata
  (HU-AOS-27): no se podrá activar. 409 `ya_activa`, `es_canary` o `ya_detenida`.
- `POST /api/v1/admin/connections/{id}/rollback` — vuelve a la anterior (o a `to_release_id`).
  Efecto en el siguiente turno de cada conversación.

ADR-0117 nombra las rutas como `/api/v1/connections/{id}/...`; viven bajo `/admin` por el realm
(ADR-0077 §2), como `connection_agents`.

Permisos: lectura con la sección `connections`; construir con `connections` en escritura;
promover y volver atrás, además, propietario o master (cambian lo que atiende a clientes reales).
Mientras el negocio esté en `aos_runtime = 'legacy'` las escrituras quedan para el master (403
`aos_runtime_legacy`), como en `agents`: la release no hace nada hasta `v3`. 404 si la conexión
o la release no son del negocio.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import DBAPIError

from core.admin.roles import actua_como_master, es_master
from core.agents.versiones import escritura_habilitada
from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.releases import build as svc_build
from core.releases import manifiesto as svc_manifiesto
from core.releases import promocion as svc
from core.shared.logger import get_logger

log = get_logger(__name__)
# Motivos que escribe el sistema: la API no los acepta (así la auditoría no se confunde).
# Motivos que escriben los caminos internos (exentos del gate, HU-AOS-21): la API no los acepta,
# aunque la exención no se deduce del motivo sino de un parámetro que la API nunca envía.
_MOTIVOS_RESERVADOS = frozenset(
    {
        "rollback", "release_1", "yanked", "hotfix_yanked", "migrar_a_v3", "qa_datos",
        # HU-AOS-32: los del canary.
        "canary", "no_inferioridad", "senal_dura", "reemplazado", "habilidad_retirada",
    }
)


def _motivo_libre(valor: str) -> str:
    if valor in _MOTIVOS_RESERVADOS:
        raise ValueError("motivo reservado al sistema")
    return valor


router = APIRouter(prefix="/admin/connections", tags=["admin · releases"])

Origin = Literal["owner", "platform"]


class ReleaseSummary(BaseModel):
    id: uuid.UUID
    number: int
    origin: Origin
    manifest_hash: str
    created_at: datetime
    active: bool
    canary: bool
    last_event: str | None
    last_event_at: datetime | None
    lifecycle: str | None = Field(
        default=None, description="Último evento de ciclo de vida: promoted, rolled_back, invalid…"
    )
    detenida: bool = Field(
        default=False,
        description="Bloqueada: la detuvo el dueño (como candidata) o el sistema la retiró de una prueba por ir peor. No se puede activar.",
    )


class ReleaseEvent(BaseModel):
    type: str
    data: dict[str, Any]
    actor: str
    created_at: datetime


class ReleaseDetail(ReleaseSummary):
    manifest: dict[str, Any] = Field(
        description="Manifiesto `release/1` sin textos: hashes y tamaños."
    )
    events: list[ReleaseEvent]


class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str = Field(default="", max_length=500)


class BuildOut(BaseModel):
    release: ReleaseSummary
    created: bool


class PromoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="manual", pattern=r"^[a-z_]{1,40}$")
    # HU-AOS-21: solo el master puede saltar el gate de pass^k, con un motivo escrito.
    forzar: bool = False
    motivo_forzado: str | None = Field(default=None, max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, valor: str) -> str:
        return _motivo_libre(valor)


class EvaluationRequested(BaseModel):
    estado: str = Field(
        description="`encolada`, `en_curso`, `demasiados_intentos`, `no_encolada`, `sin_cambios`, `ya_medida`."
    )


class EvaluationVerdict(BaseModel):
    aprobado: bool
    motivo: str
    k: int | None
    umbral: float | None
    por_procedimiento: dict[str, dict[str, Any]]
    sin_medir: list[str]


class EvaluationRun(BaseModel):
    estado: str
    codigo: str | None
    creado: str | None
    sin_escenarios: list[str]


class Evaluation(BaseModel):
    gate_cerrado: bool
    base_release_id: uuid.UUID | None
    es_la_activa: bool
    veredicto: EvaluationVerdict | None
    medicion: EvaluationRun | None


class AgentChange(BaseModel):
    key: str
    nombre: str
    rol: str
    cambio: str = Field(description="`nuevo`, `retirado`, `actualizado` o `por_defecto`.")
    version_antes: int | None
    version_ahora: int | None
    por_defecto: bool | None = None
    secciones: list[str] = Field(default=[], description="Claves de primer nivel del contenido que cambiaron.")
    herramientas_agregadas: list[str] = []
    herramientas_quitadas: list[str] = []
    procedimientos_agregados: list[str] = []
    procedimientos_quitados: list[str] = []


class SkillChange(BaseModel):
    pack_id: str
    nombre: str
    cambio: str = Field(description="`agregada`, `quitada` o `nueva_version`.")
    antes: str | None
    ahora: str | None


class SuperpowersDiff(BaseModel):
    habilidades: list[SkillChange]
    plugins_agregados: list[str]
    plugins_quitados: list[str]


class PermissionChange(BaseModel):
    desde: str
    hacia: str
    cambio: str = Field(description="`agregado`, `quitado` o `modos`.")
    modos_antes: list[str]
    modos_ahora: list[str]


class OtherChanges(BaseModel):
    ajustes: list[str] = Field(description="Nombres de los ajustes del asistente que cambiaron.")
    playbooks: list[str]
    flujo: bool


class MeasuredProcedure(BaseModel):
    procedure_id: str
    titulo: str | None
    motivo: str


class ReleaseDiff(BaseModel):
    candidata: int
    base: int | None
    sin_linea_base: bool
    agentes: list[AgentChange]
    superpoderes: SuperpowersDiff
    permisos: list[PermissionChange]
    otros: OtherChanges
    medira: list[MeasuredProcedure]


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_release_id: uuid.UUID | None = None
    reason: str = Field(default="manual", pattern=r"^[a-z_]{1,40}$")

    @field_validator("reason")
    @classmethod
    def _reason(cls, valor: str) -> str:
        return _motivo_libre(valor)


class ReleaseChange(BaseModel):
    connection_id: uuid.UUID
    from_release_id: uuid.UUID | None
    to_release_id: uuid.UUID


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _puede_escribir(principal: Principal) -> uuid.UUID:
    tid = _tenant(principal)
    if not actua_como_master(principal.admin) and not await escritura_habilitada(tid):
        raise ApiError(
            403,
            "aos_runtime_legacy",
            "Las versiones se activan con el Agent OS de este negocio: por ahora solo las gestiona el administrador.",
        )
    return tid


def _actor(principal: Principal) -> str:
    return f"admin:{principal.admin.id}"


def _summary(v: svc.ReleaseVista) -> ReleaseSummary:
    return ReleaseSummary(
        id=v.id,
        number=v.number,
        origin=v.origin,  # type: ignore[arg-type]
        manifest_hash=v.manifest_hash,
        created_at=v.created_at,
        active=v.active,
        canary=v.canary,
        last_event=v.last_event,
        last_event_at=v.last_event_at,
        lifecycle=v.lifecycle,
        detenida=v.detenida,
    )


def _error_de_base(exc: DBAPIError, operacion: str) -> ApiError:
    # Sin `str(exc)`: SQLAlchemy imprime los parámetros (el manifiesto, el contenido del agente).
    log.error("release_api_db_error", operation=operacion, error=type(exc.orig).__name__)
    return ApiError(500, "db_error", "No se pudo completar la operación. Inténtalo de nuevo.")


def _no_encontrada(exc: LookupError) -> ApiError:
    if isinstance(exc, svc_build.ConexionNoEncontrada):
        return ApiError.not_found("Conexión no encontrada.")
    return ApiError.not_found("Versión no encontrada.")


@router.get("/{connection_id}/releases", response_model=list[ReleaseSummary])
async def list_releases(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[int | None, Query(ge=1, lt=2**31)] = None,
) -> list[ReleaseSummary]:
    try:
        vistas = await svc.listar(_tenant(principal), connection_id, limite=limit, antes_de=before)
    except svc_build.ConexionNoEncontrada as exc:
        raise _no_encontrada(exc) from None
    return [_summary(v) for v in vistas]


@router.post(
    "/{connection_id}/releases", response_model=BuildOut, status_code=status.HTTP_201_CREATED
)
async def build_release(
    connection_id: uuid.UUID,
    payload: BuildIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> BuildOut:
    tid = await _puede_escribir(principal)
    try:
        resultado = await svc_build.construir(
            tid,
            connection_id,
            actor=_actor(principal),
            created_by=principal.admin.id,
            notas=payload.notes,
        )
        vista, _, _ = await svc.obtener(tid, connection_id, resultado.release_id)
        if not resultado.creada and vista.detenida:
            # El build es idempotente: lo mismo devuelve la release que el dueño detuvo.
            raise ApiError(
                409,
                "version_detenida",
                "Esa combinación de agentes y ajustes se detuvo antes. Cambia algo para crear una versión nueva.",
            )
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except DBAPIError as exc:
        raise _error_de_base(exc, "build") from None
    except svc_build.ErrorDelBuild as exc:
        raise ApiError(
            422,
            exc.codigo,
            str(exc),
            fields={f"detalle[{i}]": d for i, d in enumerate(exc.detalle[:10])},
        ) from None
    return BuildOut(release=_summary(vista), created=resultado.creada)


@router.get("/{connection_id}/releases/{release_id}", response_model=ReleaseDetail)
async def get_release(
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ReleaseDetail:
    try:
        vista, manifest, eventos = await svc.obtener(_tenant(principal), connection_id, release_id)
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    return ReleaseDetail(
        **_summary(vista).model_dump(),
        manifest=svc_manifiesto.resumen(manifest),
        events=[ReleaseEvent(**e) for e in eventos],
    )


@router.post("/{connection_id}/releases/{release_id}/promote", response_model=ReleaseChange)
async def promote_release(
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    payload: PromoteIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ReleaseChange:
    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    forzado: str | None = None
    if payload.forzar:
        if not es_master(principal.admin):
            raise ApiError(403, "solo_master", "Solo el master puede saltar el gate de pass^k.")
        forzado = (payload.motivo_forzado or "").strip()
        if not forzado:
            raise ApiError(422, "motivo_requerido", "Para saltar el gate hay que escribir el motivo.")
    try:
        cambio = await svc.promover(
            tid,
            connection_id,
            release_id,
            actor=_actor(principal),
            motivo=payload.reason,
            forzado_por_master=forzado,
        )
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except DBAPIError as exc:
        raise _error_de_base(exc, "release") from None
    except svc.ConflictoDeRelease as exc:
        details = dict(exc.details)
        if exc.codigo == "gate_pendiente":
            # Fuera del FOR UPDATE (el raise ya soltó la transacción): encola la medición.
            from core.releases import gate

            # `estado`: la última medición; `encolado`: qué pasó con este pedido.
            details["encolado"] = await gate.solicitar_medicion(tid, connection_id, release_id, actor=_actor(principal))
        raise ApiError(409, exc.codigo, str(exc), details=details or None) from None
    return ReleaseChange(
        connection_id=cambio.connection_id,
        from_release_id=cambio.from_release_id,
        to_release_id=cambio.to_release_id,
    )


@router.post(
    "/{connection_id}/releases/{release_id}/evaluate",
    response_model=EvaluationRequested,
    responses={202: {"model": EvaluationRequested}},
)
async def evaluate_release(
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    response: Response,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> EvaluationRequested:
    """Pide medir la candidata contra la activa (HU-AOS-21 parte 4). Exige la bandera del gate del
    negocio (409 `gate_abierto` si está apagada) para no gastar el tope sin gate."""
    from core.releases import gate

    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    try:
        estado = await gate.pedir_evaluacion(tid, connection_id, release_id, actor=_actor(principal))
    except LookupError:
        raise ApiError.not_found("Versión no encontrada.") from None
    if estado == "ya_activa":
        raise ApiError(409, "ya_activa", "Esa versión ya es la activa.")
    if estado == "gate_abierto":
        raise ApiError(409, "gate_abierto", "El gate de pass^k no está activo en este negocio.")
    if estado == "sin_release_activa":
        raise ApiError(409, "sin_release_activa", "La conexión no tiene versión activa: la primera no se mide.")
    if estado in ("encolada", "en_curso"):
        response.status_code = status.HTTP_202_ACCEPTED
    return EvaluationRequested(estado=estado)


@router.get("/{connection_id}/releases/{release_id}/evaluation", response_model=Evaluation)
async def get_evaluation(
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> Evaluation:
    """La medición de la candidata contra la activa de ahora (HU-AOS-21 parte 4)."""
    from core.releases import gate

    try:
        datos = await gate.estado_de_evaluacion(_tenant(principal), connection_id, release_id)
    except LookupError:
        raise ApiError.not_found("Versión no encontrada.") from None
    return Evaluation.model_validate(datos)


@router.get("/{connection_id}/releases/{release_id}/diff", response_model=ReleaseDiff)
async def get_release_diff(
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
    against: uuid.UUID | None = None,
) -> ReleaseDiff:
    """Qué cambia la release frente a `against` (por defecto, la activa) de la MISMA conexión
    (HU-AOS-27). 404 si alguna no es de la conexión."""
    from core.releases import diffs

    try:
        datos = await diffs.de_la_conexion(_tenant(principal), connection_id, release_id, against)
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except DBAPIError as exc:
        raise _error_de_base(exc, "diff") from None
    return ReleaseDiff.model_validate(datos)


@router.post("/{connection_id}/releases/{release_id}/stop", response_model=ReleaseSummary)
async def stop_release(
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ReleaseSummary:
    """El dueño detiene una candidata (HU-AOS-27): queda retirada y no se podrá activar."""
    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    try:
        await svc.detener(tid, connection_id, release_id, actor=_actor(principal))
        vista, _, _ = await svc.obtener(tid, connection_id, release_id)
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except DBAPIError as exc:
        raise _error_de_base(exc, "release") from None
    except svc.ConflictoDeRelease as exc:
        raise ApiError(409, exc.codigo, str(exc)) from None
    return _summary(vista)


@router.post("/{connection_id}/rollback", response_model=ReleaseChange)
async def rollback_release(
    connection_id: uuid.UUID,
    payload: RollbackIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ReleaseChange:
    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    try:
        cambio = await svc.rollback(
            tid,
            connection_id,
            actor=_actor(principal),
            to_release_id=payload.to_release_id,
            motivo=payload.reason,
        )
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except DBAPIError as exc:
        raise _error_de_base(exc, "release") from None
    except svc.ConflictoDeRelease as exc:
        raise ApiError(409, exc.codigo, str(exc)) from None
    return ReleaseChange(
        connection_id=cambio.connection_id,
        from_release_id=cambio.from_release_id,
        to_release_id=cambio.to_release_id,
    )


# ---------------------------------------------------------------- canary (HU-AOS-32)
class CanaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percent: int = Field(ge=1, le=50, description="Porcentaje de las conversaciones nuevas que prueban la versión.")
    forzar: bool = Field(default=False, description="Solo master: saltar el gate de pass^k (con motivo).")
    motivo_forzado: str | None = Field(default=None, max_length=200)


class CanaryPercentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percent: int = Field(ge=1, le=50)


class CanaryLane(BaseModel):
    conversaciones: int
    escalamiento: float | None = Field(description="Fracción de 0 a 1 de conversaciones que pasaron a una persona.")
    completitud: float | None = Field(description="Fracción de 0 a 1 de recorridos completados (los que empezaron en la prueba).")
    tareas_terminadas: int
    costo_por_conversacion_usd: float | None = Field(
        description="USD por conversación; null sin datos o sin la sección `costs` (ver `costos_visibles`)."
    )
    p95_ms: int | None


class CanarySignal(BaseModel):
    activa: bool
    evaluadas: int
    llegaron_mal: int
    tasa: float | None
    tasa_estable: float | None
    sin_resultado: int


class CanaryState(BaseModel):
    release_id: uuid.UUID
    release_number: int | None
    base_release_id: uuid.UUID | None
    base_release_number: int | None
    percent: int = Field(description="Porcentaje vigente de las conversaciones nuevas que prueban la versión.")
    started_at: datetime
    horas: float = Field(description="Horas desde el inicio de la prueba.")
    umbral_conversaciones: int = Field(description="Conversaciones en la versión nueva para juntar la muestra.")
    umbral_horas: int = Field(description="Horas mínimas para juntar la muestra.")
    dias_manual: int
    publicable_desde: datetime = Field(description="Desde cuándo se puede publicar a mano sin muestra.")
    veredicto: Literal["sin_muestra", "peor", "listo"]
    motivos: list[Literal["escalamiento", "completitud"]]
    advertencias: list[Literal["costo", "lentitud"]]
    costos_visibles: bool
    veredicto_guardado: bool
    senal_dura: CanarySignal
    puede_publicar: bool
    motivo_no_publicar: Literal["senal_dura", "peor", "sin_muestra"] | None
    estable: CanaryLane
    canary: CanaryLane


async def _ver_costos(principal: Principal) -> bool:
    from core.admin.permissions import effective_access

    return await effective_access(principal.admin, "costs", principal.tenant_id) is not None


def _lane(c: Any, ver_costos: bool) -> CanaryLane:
    return CanaryLane(
        conversaciones=c.conversaciones,
        escalamiento=c.escalamiento,
        completitud=c.completitud,
        tareas_terminadas=c.tareas_terminadas,
        costo_por_conversacion_usd=c.costo_por_conversacion if ver_costos else None,
        p95_ms=c.p95_ms,
    )


def _conflicto(exc: svc.ConflictoDeRelease) -> ApiError:
    return ApiError(409, exc.codigo, str(exc), details=dict(exc.details) or None)


@router.post("/{connection_id}/releases/{release_id}/canary", status_code=204)
async def start_canary(
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    payload: CanaryIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> None:
    """Prueba la versión con una parte de las conversaciones nuevas (HU-AOS-32). Mismas reglas que
    publicar: dueño o master, gate aprobado si está cerrado, `aos_runtime = v3` (409 `requiere_v3`).
    Hasta la parte 2 (la reversión automática) responde 409 `canary_no_disponible`."""
    from core.releases import canary

    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    forzado: str | None = None
    if payload.forzar:
        if not es_master(principal.admin):
            raise ApiError(403, "solo_master", "Solo el master puede saltar el gate de pass^k.")
        forzado = (payload.motivo_forzado or "").strip()
        if not forzado:
            raise ApiError(422, "motivo_requerido", "Para saltar el gate hay que escribir el motivo.")
    try:
        await canary.iniciar(
            tid, connection_id, release_id, percent=payload.percent, actor=_actor(principal), forzado_por_master=forzado
        )
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except DBAPIError as exc:
        raise _error_de_base(exc, "canary") from None
    except svc.ConflictoDeRelease as exc:
        details = dict(exc.details)
        if exc.codigo == "gate_pendiente":
            from core.releases import gate

            details["encolado"] = await gate.solicitar_medicion(tid, connection_id, release_id, actor=_actor(principal))
        raise ApiError(409, exc.codigo, str(exc), details=details or None) from None


@router.patch("/{connection_id}/canary", status_code=204)
async def change_canary_percent(
    connection_id: uuid.UUID,
    payload: CanaryPercentIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> None:
    """Cambia el porcentaje de la prueba (rige al empezar cada episodio)."""
    from core.releases import canary

    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    try:
        await canary.cambiar_porcentaje(tid, connection_id, percent=payload.percent, actor=_actor(principal))
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except svc.ConflictoDeRelease as exc:
        raise _conflicto(exc) from None


@router.post("/{connection_id}/canary/stop", status_code=204)
async def stop_canary(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> None:
    """Detiene la prueba (siempre como `detenida_por_dueno`: la versión se puede volver a probar)."""
    from core.releases import canary

    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    try:
        await canary.detener(tid, connection_id, actor=_actor(principal))
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except svc.ConflictoDeRelease as exc:
        raise _conflicto(exc) from None


@router.post("/{connection_id}/canary/promote", response_model=ReleaseChange)
async def promote_canary(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ReleaseChange:
    """Publica para todos la versión en prueba: exige el veredicto `listo` (o 7 días sin muestra) y
    que no haya señal dura (409 `canary_peor`, `canary_sin_muestra`, `canary_senal_dura`)."""
    from core.releases import canary

    require_owner_or_master(principal)
    tid = await _puede_escribir(principal)
    try:
        cambio = await canary.publicar(tid, connection_id, actor=_actor(principal))
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    except DBAPIError as exc:
        raise _error_de_base(exc, "canary") from None
    except svc.ConflictoDeRelease as exc:
        raise _conflicto(exc) from None
    return ReleaseChange(
        connection_id=cambio.connection_id, from_release_id=cambio.from_release_id, to_release_id=cambio.to_release_id
    )


@router.get("/{connection_id}/canary", response_model=CanaryState | None)
async def get_canary(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> CanaryState | None:
    """La prueba en curso de la conexión con su muestra, la comparación por carril, el veredicto y la
    señal dura; null si no hay. No escribe nada."""
    from core.releases import canary

    try:
        e = await canary.estado(_tenant(principal), connection_id)
    except LookupError as exc:
        raise _no_encontrada(exc) from None
    if e is None:
        return None
    ver = await _ver_costos(principal)
    d = e.senal_dura
    from datetime import timedelta

    return CanaryState(
        release_id=e.release_id,
        release_number=e.release_number,
        base_release_id=e.base_release_id,
        base_release_number=e.base_release_number,
        percent=e.percent,
        started_at=e.desde,
        horas=round(e.horas, 1),
        umbral_conversaciones=int(e.umbrales["muestra_conversaciones"]),
        umbral_horas=int(e.umbrales["muestra_horas"]),
        dias_manual=int(e.umbrales["dias_manual"]),
        publicable_desde=e.desde + timedelta(days=int(e.umbrales["dias_manual"])),
        veredicto=e.veredicto.estado,  # type: ignore[arg-type]
        motivos=e.veredicto.motivos,  # type: ignore[arg-type]
        advertencias=e.veredicto.advertencias,  # type: ignore[arg-type]
        costos_visibles=ver,
        veredicto_guardado=e.guardado,
        senal_dura=CanarySignal(
            activa=d.activa, evaluadas=d.evaluadas, llegaron_mal=d.llegaron_mal, tasa=d.tasa,
            tasa_estable=d.tasa_estable, sin_resultado=d.sin_resultado,
        ),
        puede_publicar=e.puede_publicar,
        motivo_no_publicar=e.motivo_no_publicar,  # type: ignore[arg-type]
        estable=_lane(e.estable, ver),
        canary=_lane(e.canary, ver),
    )


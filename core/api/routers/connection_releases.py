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

from fastapi import APIRouter, Depends, Query, status
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
    {"rollback", "release_1", "yanked", "hotfix_yanked", "migrar_a_v3", "qa_datos"}
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
        raise ApiError(409, exc.codigo, str(exc)) from None
    return ReleaseChange(
        connection_id=cambio.connection_id,
        from_release_id=cambio.from_release_id,
        to_release_id=cambio.to_release_id,
    )


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

"""Instalaciones de habilidades por conexión (ADR-0118 §2, ADR-0077, HU-AOS-16 parte 1).

- `GET    /api/v1/admin/skills/installations[?connection_id]` — instalaciones vivas del negocio
  (sin la `core` de secretos): `{id, pack_id, scope, connection_id, status, installed_at}`.
- `POST   /api/v1/admin/skills/installations` — `{pack_id, connection_id}`: instala la habilidad
  solo para esa conexión (scope `connection`). 201 creada; 200 si ya estaba (idempotente).
- `DELETE /api/v1/admin/skills/installations/{id}` — la quita (borrado lógico).

Semántica (parte 1): el scope de negocio sigue siendo la habilitación de siempre
(`/admin/plugins/{id}/action`, `spec.packs`); una conexión con instalaciones propias congela en
su próxima release SOLO esas (reemplazo entero, sin merge). Precondición: el pack está
habilitado en el negocio (409 `pack_not_enabled`). La instalación no lleva versión: la fija la
release. Instalar o quitar aplica con la próxima release de la conexión.

Permisos: lectura con la sección `plugins`; escritura con `plugins` en escritura y, como las
instancias de agentes, solo el master mientras el negocio esté en `aos_runtime = 'legacy'`
(403 `aos_runtime_legacy`): sin v3 la release no se lee. 404 si la conexión o la instalación
no son del negocio; 409 `connection_not_conversational` para una integración.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from core.admin.roles import actua_como_master
from core.agents import instalaciones as svc
from core.agents.spec import enabled_business_packs, load_active_spec
from core.agents.versiones import escritura_habilitada
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.plugin_manager import plugin_manager

router = APIRouter(prefix="/admin/skills", tags=["admin · habilidades"])

_APLICA = " Aplica con la próxima release de la conexión."


class SkillInstallation(BaseModel):
    id: uuid.UUID
    pack_id: str
    scope: Literal["tenant", "connection"]
    connection_id: uuid.UUID | None
    status: str
    installed_at: datetime


class SkillInstallationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(pattern=r"^[a-z0-9_]{1,60}$")
    # Solo scope de conexión en esta parte: el de negocio es la habilitación de siempre.
    scope: Literal["connection"] = "connection"
    connection_id: uuid.UUID


def _vista(i: svc.InstalacionVista) -> SkillInstallation:
    return SkillInstallation(
        id=i.id,
        pack_id=i.pack_id,
        scope=i.scope,  # type: ignore[arg-type]
        connection_id=i.connection_id,
        status=i.status,
        installed_at=i.installed_at,
    )


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
            "Las habilidades por conexión se activan con el Agent OS de este negocio: por ahora solo las edita el administrador.",
        )
    return tid


def _error(exc: Exception) -> ApiError:
    if isinstance(exc, svc.ConexionNoEncontrada):
        return ApiError.not_found("Conexión no encontrada.")
    if isinstance(exc, svc.InstalacionNoEncontrada):
        return ApiError.not_found("Instalación no encontrada.")
    if isinstance(exc, svc.ConflictoInstalacion):
        return ApiError(409, exc.code, str(exc))
    raise exc


@router.get("/installations", response_model=list[SkillInstallation])
async def list_installations(
    principal: Annotated[Principal, Depends(require("plugins"))],
    connection_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[SkillInstallation]:
    tid = _tenant(principal)
    return [_vista(i) for i in await svc.listar(tid, connection_id)]


@router.post(
    "/installations",
    response_model=SkillInstallation,
    responses={201: {"model": SkillInstallation}},
    description="Instala una habilidad solo para una conexión; 200 si ya estaba." + _APLICA,
)
async def create_installation(
    payload: SkillInstallationCreate,
    response: Response,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> SkillInstallation:
    tid = await _puede_escribir(principal)
    if plugin_manager.get(payload.pack_id) is None:
        raise ApiError.not_found("Habilidad no encontrada.")
    habilitados = set(enabled_business_packs(await load_active_spec()))
    try:
        vista, creada = await svc.instalar_en_conexion(
            tid,
            payload.connection_id,
            payload.pack_id,
            habilitados=habilitados,
            actor=principal.admin.id,
        )
    except (svc.ConexionNoEncontrada, svc.ConflictoInstalacion) as exc:
        raise _error(exc) from None
    response.status_code = status.HTTP_201_CREATED if creada else status.HTTP_200_OK
    return _vista(vista)


@router.delete("/installations/{installation_id}", status_code=204, description="Quita la instalación." + _APLICA)
async def delete_installation(
    installation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> Response:
    tid = await _puede_escribir(principal)
    try:
        await svc.desinstalar(tid, installation_id)
    except svc.InstalacionNoEncontrada as exc:
        raise _error(exc) from None
    return Response(status_code=204)

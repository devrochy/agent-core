"""Instancias de agentes de una conexión (ADR-0112 §1, ADR-0077, HU-AOS-13).

- `GET    /api/v1/admin/connections/{connection_id}/agents` — instancias vivas de la conexión
  del negocio: el Coordinador primero y luego los especialistas por prioridad. `latest_version`
  es la última versión publicada del agente, no la que corre: la release activa llega con
  HU-AOS-14. 404 si la conexión no es del negocio.
- `POST   /api/v1/admin/connections/{connection_id}/agents` — `{agent_id, priority?, enabled?,
  is_default?}`: agrega un especialista (con versión publicada) a la conexión.
- `PATCH  /api/v1/admin/connections/{connection_id}/agents/{instance_id}` — `{enabled?,
  priority?, is_default?: true}`: habilita o deshabilita, cambia la prioridad o lo vuelve el
  agente por defecto (el anterior deja de serlo).
- `DELETE /api/v1/admin/connections/{connection_id}/agents/{instance_id}` — lo quita (borrado
  lógico).

Reglas (409 con `code` estable): el Coordinador lo gestiona el sistema (backfill y altas): no se
agrega, no se deshabilita ni se quita por aquí (`not_specialist`, `core_required`); la conexión
tiene que tenerlo antes de sumar especialistas (`core_missing`). Siempre hay un agente por
defecto y está habilitado (`default_disabled`, `default_required`). 422
`connection_not_conversational` para una integración.

Permisos: el GET pide la sección `connections` (lectura); las escrituras, `settings` con
escritura, como la API de agentes (asignar o habilitar un agente cambia lo que dice el
asistente), y además poder ver `connections`. El GET muestra key, nombre y última versión de
los agentes a quien ve la conexión: es lo que la consola de conexiones necesita, sin contenido. Además, mientras el runtime del negocio sea `legacy` (ADR-0112) las instancias no
hacen nada, así que solo escriben el master o los negocios con `aos_runtime = 'v3'` (403
`aos_runtime_legacy`). Con v3, los cambios aplican con la próxima release (HU-AOS-14).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from core.admin.permissions import effective_access
from core.admin.roles import actua_como_master
from core.agents import instancias as svc
from core.agents.versiones import escritura_habilitada
from core.api.deps import Principal, require
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/connections", tags=["admin · conexiones"])

_APLICA = " Con el Agent OS (runtime v3), el cambio aplica con la próxima release de la conexión."


class ConnectionAgentInstance(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    name: str
    role: Literal["core", "specialist", "backoffice"]
    is_default: bool
    priority: int
    enabled: bool
    latest_version: int | None


class ConnectionAgentCreate(BaseModel):
    # Sin rol, conexión ni negocio en el cuerpo: el rol es el `kind` del agente.
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    priority: int = Field(default=svc.PRIORIDAD_POR_DEFECTO, ge=0, le=1000)
    enabled: bool = True
    is_default: bool = False


class ConnectionAgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    # Solo `true`: el agente por defecto se cambia eligiendo otro, nunca queda ninguno.
    is_default: Literal[True] | None = None


def _vista(i: svc.InstanciaVista) -> ConnectionAgentInstance:
    return ConnectionAgentInstance(
        id=i.id,
        agent_id=i.agent_id,
        key=i.key,
        name=i.name,
        role=i.role,  # type: ignore[arg-type]
        is_default=i.is_default,
        priority=i.priority,
        enabled=i.enabled,
        latest_version=i.latest_version,
    )


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _puede_escribir(principal: Principal) -> uuid.UUID:
    tid = _tenant(principal)
    if actua_como_master(principal.admin):
        return tid
    # Además de `settings` con escritura, ver la conexión (`connections`): nadie cambia los
    # agentes de una conexión que no puede ver.
    if await effective_access(principal.admin, "connections", tid) is None:
        raise ApiError.forbidden("Sin permiso para la sección 'connections'.")
    if not await escritura_habilitada(tid):
        raise ApiError(
            403,
            "aos_runtime_legacy",
            "Los agentes se activan con el Agent OS de este negocio: por ahora solo los edita el administrador.",
        )
    return tid


def _error(exc: Exception) -> ApiError:
    if isinstance(exc, svc.ConexionNoEncontrada):
        return ApiError.not_found("Conexión no encontrada.")
    if isinstance(exc, svc.InstanciaNoEncontrada):
        return ApiError.not_found("Agente de la conexión no encontrado.")
    if isinstance(exc, svc.AgenteNoInstanciable):
        return ApiError.not_found("Agente no encontrado.")
    if isinstance(exc, svc.ConexionNoConversacional):
        return ApiError(422, "connection_not_conversational", "Esta conexión es una integración: no lleva agentes.")
    if isinstance(exc, svc.ConflictoInstancia):
        return ApiError(409, exc.code, str(exc))
    raise exc


_ERRORES = (
    svc.ConexionNoEncontrada,
    svc.InstanciaNoEncontrada,
    svc.AgenteNoInstanciable,
    svc.ConexionNoConversacional,
    svc.ConflictoInstancia,
)


@router.get("/{connection_id}/agents", response_model=list[ConnectionAgentInstance])
async def list_connection_agents(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> list[ConnectionAgentInstance]:
    try:
        instancias = await svc.listar_instancias(_tenant(principal), connection_id)
    except svc.ConexionNoEncontrada:
        raise ApiError.not_found("Conexión no encontrada.") from None
    return [_vista(i) for i in instancias]


@router.post(
    "/{connection_id}/agents",
    response_model=ConnectionAgentInstance,
    status_code=status.HTTP_201_CREATED,
    description="Agrega un especialista a la conexión." + _APLICA,
)
async def add_connection_agent(
    connection_id: uuid.UUID,
    payload: ConnectionAgentCreate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> ConnectionAgentInstance:
    tid = await _puede_escribir(principal)
    try:
        nueva = await svc.agregar_instancia(
            tid,
            connection_id,
            payload.agent_id,
            priority=payload.priority,
            enabled=payload.enabled,
            is_default=payload.is_default,
            actor=principal.admin.id,
        )
        return _vista(await svc.obtener_instancia(tid, connection_id, nueva))
    except _ERRORES as exc:
        raise _error(exc) from None


@router.patch(
    "/{connection_id}/agents/{instance_id}",
    response_model=ConnectionAgentInstance,
    description="Habilita o deshabilita, cambia la prioridad o vuelve por defecto a un agente de la conexión."
    + _APLICA,
)
async def patch_connection_agent(
    connection_id: uuid.UUID,
    instance_id: uuid.UUID,
    payload: ConnectionAgentPatch,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> ConnectionAgentInstance:
    tid = await _puede_escribir(principal)
    try:
        await svc.actualizar_instancia(
            tid,
            connection_id,
            instance_id,
            enabled=payload.enabled,
            priority=payload.priority,
            is_default=payload.is_default,
            actor=principal.admin.id,
        )
        return _vista(await svc.obtener_instancia(tid, connection_id, instance_id))
    except _ERRORES as exc:
        raise _error(exc) from None


@router.delete(
    "/{connection_id}/agents/{instance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    description="Quita un especialista de la conexión (borrado lógico)." + _APLICA,
)
async def delete_connection_agent(
    connection_id: uuid.UUID,
    instance_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> Response:
    tid = await _puede_escribir(principal)
    try:
        await svc.quitar_instancia(tid, connection_id, instance_id, actor=principal.admin.id)
    except _ERRORES as exc:
        raise _error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)

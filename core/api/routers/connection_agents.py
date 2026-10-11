"""Instancias de agentes de una conexión (ADR-0112 §1, ADR-0077, HU-AOS-13).

- `GET /api/v1/admin/connections/{connection_id}/agents` — instancias vivas de la conexión del
  negocio: el Coordinador primero y luego los especialistas por prioridad. `latest_version` es
  la última versión publicada del agente, no la que corre: la release activa llega con
  HU-AOS-14. 404 si la conexión no es del negocio.

Solo lectura (sección `connections`). Agregar o quitar especialistas, prioridad y overrides
quedan para la segunda parte (consola de conexiones). Mientras el runtime del negocio sea
`legacy` (ADR-0112), las instancias no hacen nada.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.agents.instancias import ConexionNoEncontrada, listar_instancias
from core.api.deps import Principal, require
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/connections", tags=["admin · conexiones"])


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


@router.get("/{connection_id}/agents", response_model=list[ConnectionAgentInstance])
async def list_connection_agents(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> list[ConnectionAgentInstance]:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    try:
        instancias = await listar_instancias(principal.tenant_id, connection_id)
    except ConexionNoEncontrada:
        raise ApiError.not_found("Conexión no encontrada.") from None
    return [
        ConnectionAgentInstance(
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
        for i in instancias
    ]

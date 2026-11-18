"""Config de Captar leads del negocio (ADR-0193 §6, HU-AOS-70 parte 3).

`GET/PUT /api/v1/admin/captar-leads/config`: el intake del formulario de la página (activarlo, el secreto, los
orígenes permitidos y la llave de Turnstile) y el reenganche y los avisos de seguimiento. Vive en `lead_settings`
(`core.leads.config`), no en la config de un pack: un negocio sin comercio también la tiene.

Misma forma que la config de una habilidad (`/admin/plugins/{id}/config`) para que el panel use el mismo
formulario: esquema JSON, valores y `configured_secrets`. Los secretos nunca salen del backend: se redactan al leer
y, si el panel los manda vacíos, se conserva el guardado. Exige el permiso de la sección `plugins`, el mismo con el
que se editaba en la config de commerce.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.forms import coerce_schema_values, redact_secrets
from core.db.session import get_session
from core.leads import config as config_leads
from core.shared.validacion import MENSAJE_CAMPOS, campos_de

router = APIRouter(prefix="/admin/captar-leads", tags=["admin · leads"])


class CaptarLeadsConfigResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    configured: bool = Field(
        description="El negocio ya tiene config de Captar leads (si no, se muestran los valores por defecto)"
    )
    schema_: dict[str, Any] = Field(alias="schema")
    values: dict[str, Any]
    configured_secrets: list[str]


class CaptarLeadsConfigSave(BaseModel):
    values: dict[str, Any]


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Elige un negocio para ver su config de Captar leads.", "tenant_required")
    return principal.tenant_id


def _respuesta(config: config_leads.ConfigCaptarLeads | None) -> CaptarLeadsConfigResponse:
    schema = config_leads.ConfigCaptarLeads.model_json_schema()
    values = config_leads.a_guardar(config or config_leads.ConfigCaptarLeads())
    redacted, configured = redact_secrets(schema, values)
    return CaptarLeadsConfigResponse(
        configured=config is not None, schema_=schema, values=redacted, configured_secrets=configured
    )


@router.get("/config", response_model=CaptarLeadsConfigResponse)
async def get_config(
    principal: Annotated[Principal, Depends(require("plugins"))],
) -> CaptarLeadsConfigResponse:
    tenant_id = _tenant(principal)
    async with get_session() as session:
        config = await config_leads.cargar(session, tenant_id)
    return _respuesta(config)


@router.put("/config", response_model=CaptarLeadsConfigResponse)
async def save_config(
    payload: CaptarLeadsConfigSave,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> CaptarLeadsConfigResponse:
    """Reemplaza la config del negocio. Un campo que no llega toma su valor por defecto, salvo los secretos, que
    conservan el guardado (también si llegan vacíos). Valores inválidos: 422 `invalid_config` con `fields`."""
    tenant_id = _tenant(principal)
    schema = config_leads.ConfigCaptarLeads.model_json_schema()
    async with get_session() as session:
        actual = await config_leads.cargar(session, tenant_id)
        previous = config_leads.a_guardar(actual) if actual is not None else {}
        values = coerce_schema_values(schema, payload.values, previous)
        try:
            config = config_leads.ConfigCaptarLeads.model_validate(values)
        except ValidationError as exc:
            raise ApiError(422, "invalid_config", MENSAJE_CAMPOS, fields=campos_de(exc.errors())) from exc
        await config_leads.guardar(session, tenant_id, config)
    return _respuesta(config)

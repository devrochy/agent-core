"""Habilidades (plugins): listado, configuración y guardado.

- `GET /api/v1/admin/plugins` — packs disponibles y si están habilitados para el
  tenant activo (mismos datos que `/admin/plugins`).
- `GET /api/v1/admin/plugins/{plugin_id}/config` — JSON Schema de la config +
  valores actuales, con los secretos **redactados** (nunca salen del backend).
- `PUT .../config` — valida, cifra y activa el código del pack para el tenant.
- `POST .../action` — `retry` limpia la salud de error; `activate`/`deactivate` responden 410: una
  habilidad es del negocio si algún agente activo la tiene instalada (ADR-0206, HU-AOS-91).

Reusa `configure_plugin_for_tenant` de `core.agents.habilitacion` para no duplicar la lógica de activación.

Requiere la sección `plugins`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from core.agents.instalaciones_agente import packs_del_negocio
from core.agents.spec import CORE_PACK_ID
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.forms import coerce_schema_values, hide_fields, redact_secrets
from core.db.models import PluginConfig, PluginRecord, PluginTenantState
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.crypto import get_secret_box
from core.shared.exceptions import PluginConfigError
from core.shared.validacion import MENSAJE_CAMPOS

router = APIRouter(prefix="/admin/plugins", tags=["admin · habilidades"])


class PluginSummary(BaseModel):
    id: str
    name: str
    version: str
    status: str
    error_message: str | None
    has_config: bool
    enabled_for_tenant: bool
    tenant_health_error: str | None
    # Catálogo de habilidades (HU-FWK-04).
    display_name: str = ""
    category: str = ""
    description: str = ""
    required_connections: list[str] = []
    min_plan: str = "free"
    action_summary: list[str] = []
    setup_guide: str = ""


class PluginConfigResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    plugin_id: str
    name: str
    description: str | None
    schema_: dict[str, Any] = Field(alias="schema")
    values: dict[str, Any]
    configured_secrets: list[str]


class PluginConfigSave(BaseModel):
    values: dict[str, Any]


class PluginAction(BaseModel):
    action: Literal["activate", "deactivate", "retry"]


async def _stored_previous(plugin_id: str, tenant_id) -> dict[str, Any]:
    async with get_session() as session:
        stored = (
            await session.execute(
                select(PluginConfig).where(
                    PluginConfig.plugin_id == plugin_id,
                    PluginConfig.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
    if stored is None:
        return {}
    try:
        return get_secret_box().decrypt_json(stored.config_encrypted)
    except Exception:
        return {}


@router.get("", response_model=list[PluginSummary])
async def list_plugins(
    principal: Annotated[Principal, Depends(require("plugins"))],
) -> list[PluginSummary]:
    tenant_id = principal.tenant_id
    async with get_session() as session:
        records = {
            r.id: r for r in (await session.execute(select(PluginRecord))).scalars().all()
        }
        health_rows = (
            (
                await session.execute(
                    select(PluginTenantState).where(
                        PluginTenantState.tenant_id == tenant_id,
                        PluginTenantState.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    health = {h.plugin_id: h for h in health_rows}
    enabled = await packs_del_negocio()

    result: list[PluginSummary] = []
    for module in plugin_manager.list_modules():
        if module.id == CORE_PACK_ID:
            continue
        record = records.get(module.id)
        state = health.get(module.id)
        result.append(
            PluginSummary(
                id=module.id,
                name=module.name or module.id,
                version=module.version,
                status=record.status if record else "detected",
                error_message=record.error_message if record else None,
                has_config=module.config_schema is not None,
                enabled_for_tenant=module.id in enabled,
                tenant_health_error=state.error_message if state else None,
                display_name=module.display_name or module.name or module.id,
                category=module.category,
                description=module.description,
                required_connections=list(module.required_connections),
                min_plan=module.min_plan,
                action_summary=list(module.action_summary),
                setup_guide=module.setup_guide,
            )
        )
    return result


_ACTIVACION_RETIRADA = {
    410: {
        "description": (
            "`activate`/`deactivate` retirados (ADR-0206, HU-AOS-91): `error.code = business_activation_retired`. "
            "Una habilidad es del negocio si algún agente activo la tiene instalada; se instala en el agente."
        )
    }
}


@router.post("/{plugin_id}/action", status_code=204, responses=_ACTIVACION_RETIRADA)
async def plugin_action(
    plugin_id: str,
    payload: PluginAction,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> Response:
    """Limpia la salud de error del pack para el tenant (`retry`, ADR-0024).

    `activate` y `deactivate` responden 410 `business_activation_retired`: ya no hay interruptor del negocio, la
    habilidad se instala o se quita en el agente que la usa (`/admin/agents/{id}/skills`).
    """
    # El 410 va antes de buscar el módulo: no dice qué ids existen en el proceso.
    if payload.action != "retry":
        raise ApiError(
            410,
            "business_activation_retired",
            "Las habilidades se instalan por agente: instálala en el agente que la usa.",
        )
    module = plugin_manager.get(plugin_id)
    if module is None or module.id == CORE_PACK_ID:
        raise ApiError.not_found("Habilidad no encontrada.")
    await plugin_manager.reactivate_for_tenant(principal.tenant_id, plugin_id)
    return Response(status_code=204)


@router.get("/{plugin_id}/config", response_model=PluginConfigResponse)
async def get_plugin_config(
    plugin_id: str,
    principal: Annotated[Principal, Depends(require("plugins"))],
) -> PluginConfigResponse:
    module = plugin_manager.get(plugin_id)
    if module is None or module.config_schema is None:
        raise ApiError.not_found("Habilidad no encontrada o sin configuración.")
    schema = module.config_schema.model_json_schema()

    values: dict[str, Any] = {}
    try:
        config = await plugin_manager.load_config(plugin_id)
        if config is not None:
            values = config.model_dump()
    except PluginConfigError:
        values = {}

    schema, values = hide_fields(schema, values)
    redacted, configured = redact_secrets(schema, values)
    return PluginConfigResponse(
        plugin_id=plugin_id,
        name=module.name or plugin_id,
        description=module.description,
        schema_=schema,
        values=redacted,
        configured_secrets=configured,
    )


@router.put("/{plugin_id}/config", response_model=PluginConfigResponse)
async def save_plugin_config(
    plugin_id: str,
    payload: PluginConfigSave,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> PluginConfigResponse:
    module = plugin_manager.get(plugin_id)
    if module is None or module.config_schema is None:
        raise ApiError.not_found("Habilidad no encontrada o sin configuración.")
    schema = module.config_schema.model_json_schema()

    previous = await _stored_previous(plugin_id, principal.tenant_id)
    values = coerce_schema_values(schema, payload.values, previous)

    from core.agents.habilitacion import configure_plugin_for_tenant

    """Valida, guarda y activa el código del pack. Valores inválidos (p. ej. un número negativo)
    responden 422 `invalid_config` con `fields` por campo. Ya no habilita la habilidad en el negocio:
    eso pasa al instalarla en un agente (ADR-0206, HU-AOS-91), y ahí se cobra la cuota."""
    try:
        await configure_plugin_for_tenant(plugin_id, values)
    except PluginConfigError as exc:
        raise ApiError(
            422,
            "invalid_config",
            MENSAJE_CAMPOS if exc.fields else str(exc),
            fields=exc.fields or None,
        ) from exc

    return await get_plugin_config(plugin_id, principal)

"""Habilidades (plugins): listado, configuración y guardado.

- `GET /api/v1/admin/plugins` — packs disponibles y si están habilitados para el
  tenant activo (mismos datos que `/admin/plugins`).
- `GET /api/v1/admin/plugins/{plugin_id}/config` — JSON Schema de la config +
  valores actuales, con los secretos **redactados** (nunca salen del backend).
- `PUT .../config` — valida, cifra, activa y habilita el pack para el tenant.

Reusa `configure_plugin_for_tenant` de `core.admin.views` para no duplicar la
lógica de activación/cuota (el import es local para no acoplar el paquete API).

Requiere la sección `plugins`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from core.agents.spec import CORE_PACK_ID, enabled_packs, load_active_spec
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.forms import coerce_schema_values, redact_secrets
from core.billing.enforcement import QuotaExceeded
from core.db.models import PluginConfig, PluginRecord, PluginTenantState
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.crypto import get_secret_box
from core.shared.exceptions import PluginConfigError

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
    enabled = enabled_packs(await load_active_spec())

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
            )
        )
    return result


@router.post("/{plugin_id}/action", status_code=204)
async def plugin_action(
    plugin_id: str,
    payload: PluginAction,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> Response:
    """Habilita/deshabilita el pack para el tenant, o limpia su salud de error.

    Habilitar aplica la cuota del plan; deshabilitar no toca a otros tenants; y
    `retry` reinicia la salud (ADR-0024). Reusa el servicio del panel SSR.
    """
    module = plugin_manager.get(plugin_id)
    if module is None or module.id == CORE_PACK_ID:
        raise ApiError.not_found("Habilidad no encontrada.")

    if payload.action == "retry":
        await plugin_manager.reactivate_for_tenant(principal.tenant_id, plugin_id)
        return Response(status_code=204)

    # Import local: la lógica de activación/cuota vive en el panel SSR.
    from core.admin.views import set_pack_for_tenant

    try:
        error = await set_pack_for_tenant(plugin_id, payload.action == "activate")
    except QuotaExceeded as exc:
        raise ApiError.forbidden(
            f"Tu plan no permite habilitar más habilidades ({exc.used}/{exc.limit})."
        ) from exc
    if error:
        raise ApiError.bad_request(error, "plugin_error")
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

    # Import local: la lógica de activación/cuota vive en el panel SSR.
    from core.admin.views import configure_plugin_for_tenant

    try:
        await configure_plugin_for_tenant(plugin_id, values)
    except QuotaExceeded as exc:
        raise ApiError.forbidden(
            f"Tu plan no permite habilitar más habilidades ({exc.used}/{exc.limit})."
        ) from exc
    except PluginConfigError as exc:
        raise ApiError.bad_request(str(exc), "invalid_config") from exc

    return await get_plugin_config(plugin_id, principal)

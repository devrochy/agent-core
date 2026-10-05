"""Conexiones (lectura): `GET /api/v1/admin/connections`.

Canales conectados del tenant: conexiones manuales (`channel_connections`) y las
de Meta (Embedded Signup / BYOK). Mismos datos que la vista SSR `/admin/conexiones`.
Requiere la sección `connections`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.forms import coerce_schema_values, redact_secrets
from core.db.models import PhoneNumber, utcnow
from core.db.session import get_owned, get_session
from core.gateway.connection_types import setup_url
from core.gateway.connections import list_connections
from core.shared.tenant import tenant_slug

router = APIRouter(prefix="/admin/connections", tags=["admin · conexiones"])


class ConnectionItem(BaseModel):
    id: uuid.UUID
    kind: str
    type_id: str | None = None
    origin: str
    channel: str
    label: str
    external_id: str
    active: bool
    is_primary: bool
    created_at: datetime | None
    coexistence: bool = False


class ConnectionsResponse(BaseModel):
    items: list[ConnectionItem]
    webhook_url: str


@router.get("", response_model=ConnectionsResponse)
async def list_connections_endpoint(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ConnectionsResponse:
    tenant_id = principal.tenant_id
    connections = await list_connections()

    rows = [
        ConnectionItem(
            id=c.id,
            kind="channel_connection",
            type_id=getattr(c, "type_id", None),
            origin="Manual",
            channel=c.channel,
            label=c.label or c.external_id,
            external_id=c.external_id,
            active=c.active,
            is_primary=c.is_primary,
            created_at=c.created_at,
        )
        for c in connections
    ]

    if tenant_id is not None:
        from core.db.models import MetaConnection

        async with get_session() as session:
            meta_connections = (
                (
                    await session.execute(
                        select(MetaConnection).where(
                            MetaConnection.tenant_id == tenant_id,
                            MetaConnection.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            phones: dict[uuid.UUID, str] = {}
            if meta_connections:
                phone_rows = (
                    (
                        await session.execute(
                            select(PhoneNumber).where(
                                PhoneNumber.tenant_id == tenant_id,
                                PhoneNumber.connection_id.in_([mc.id for mc in meta_connections]),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for p in phone_rows:
                    phones.setdefault(
                        p.connection_id, p.display_phone_number or p.phone_number_id
                    )

        from core.gateway.meta_credentials import origin_label

        for mc in meta_connections:
            # Del origen guardado (ADR-0094), no de comparar `meta_app_id` con la app de
            # la plataforma: una conexión manual o una BYOK que pasó a ES se rotulaban mal.
            origin = origin_label(mc.origin)
            rows.append(
                ConnectionItem(
                    id=mc.id,
                    kind="meta_connection",
                    origin=origin,
                    channel=mc.channel,
                    label=f"{mc.channel} · {mc.environment}",
                    external_id=phones.get(mc.id, mc.meta_app_id),
                    active=mc.status == "connected",
                    is_primary=False,
                    created_at=mc.created_at,
                    coexistence=bool((mc.health or {}).get("coexistence")),
                )
            )

    rows.sort(key=lambda r: r.created_at or utcnow(), reverse=True)

    slug = await tenant_slug(tenant_id) if tenant_id is not None else None
    webhook_url = ""
    if slug:
        from core.tenants import tenant_webhook_url

        webhook_url = tenant_webhook_url(slug)

    return ConnectionsResponse(items=rows, webhook_url=webhook_url)


# ------------------------------------------------------------------ catálogo y alta manual
class ConnectionTypeItem(BaseModel):
    id: str
    label: str
    description: str | None
    icon: str | None
    available: bool
    setup_kind: str
    url: str | None
    has_schema: bool


class ConnectionCategory(BaseModel):
    category: str
    label: str
    items: list[ConnectionTypeItem]


class CatalogResponse(BaseModel):
    categories: list[ConnectionCategory]


class ManualFormResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type_id: str
    label: str
    description: str | None
    schema_: dict[str, Any] = Field(alias="schema")
    values: dict[str, Any]
    configured_secrets: list[str]


class ManualSaveRequest(BaseModel):
    type_id: str
    label: str = ""
    connection_id: uuid.UUID | None = None
    values: dict[str, Any]


class ConnectionActionRequest(BaseModel):
    action: Literal["activate", "deactivate", "primary", "delete"]


@router.get("/catalog", response_model=CatalogResponse)
async def connections_catalog(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> CatalogResponse:
    """Catálogo de tipos de conexión agrupado por categoría (ADR-0069)."""
    from core.gateway.connection_types import types_by_category

    categories = [
        ConnectionCategory(
            category=group["category"],
            label=group["label"],
            items=[
                ConnectionTypeItem(
                    id=t.id,
                    label=t.label,
                    description=t.description,
                    icon=t.icon,
                    available=t.available,
                    setup_kind=t.setup_kind,
                    url=setup_url(t),
                    has_schema=bool(t.credentials_schema),
                )
                for t in group["items"]
            ],
        )
        for group in types_by_category()
    ]
    return CatalogResponse(categories=categories)


@router.get("/manual/{type_id}", response_model=ManualFormResponse)
async def connection_manual_form(
    type_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
    connection_id: uuid.UUID | None = None,
) -> ManualFormResponse:
    """JSON Schema de credenciales + valores actuales (secretos redactados)."""
    from core.gateway.connection_types import SETUP_MANUAL, get_connection_type

    ctype = get_connection_type(type_id)
    if ctype is None or not ctype.available or ctype.setup_kind != SETUP_MANUAL:
        raise ApiError.not_found("Tipo de conexión inválido.")

    values: dict[str, Any] = {}
    if connection_id is not None:
        from core.db.models import ChannelConnection
        from core.gateway.connections import credentials_of

        async with get_session() as session:
            existing = await get_owned(session, ChannelConnection, connection_id, principal.tenant_id)
        if existing is None:
            raise ApiError.not_found("Conexión no encontrada.")
        values = credentials_of(existing)

    schema = ctype.credentials_schema or {}
    redacted, configured = redact_secrets(schema, values)
    return ManualFormResponse(
        type_id=ctype.id,
        label=ctype.label,
        description=ctype.description,
        schema_=schema,
        values=redacted,
        configured_secrets=configured,
    )


@router.post("/manual", status_code=204)
async def save_manual_connection(
    payload: ManualSaveRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Alta o edición de una conexión de tipo `manual` (credenciales cifradas)."""
    from core.gateway.connection_types import SETUP_MANUAL, get_connection_type

    ctype = get_connection_type(payload.type_id)
    if ctype is None or not ctype.available or ctype.setup_kind != SETUP_MANUAL:
        raise ApiError.bad_request("Tipo de conexión inválido.", "invalid_type")

    previous: dict[str, Any] = {}
    if payload.connection_id is not None:
        from core.db.models import ChannelConnection
        from core.gateway.connections import credentials_of

        async with get_session() as session:
            existing = await get_owned(
                session, ChannelConnection, payload.connection_id, principal.tenant_id
            )
        if existing is None:
            raise ApiError.not_found("Conexión no encontrada.")
        previous = credentials_of(existing)

    schema = ctype.credentials_schema or {}
    values = coerce_schema_values(schema, payload.values, previous)
    missing = [k for k in schema.get("required", []) if not values.get(k)]
    if missing:
        raise ApiError.bad_request(f"Faltan campos: {', '.join(missing)}", "missing_fields")

    from core.gateway.connections import save_connection

    try:
        await save_connection(
            payload.label or ctype.label, values, payload.connection_id, type_id=ctype.id
        )
    except Exception as exc:
        raise ApiError.bad_request(
            f"No se pudo guardar la conexión: {str(exc)[:140]}", "save_failed"
        ) from exc
    return Response(status_code=204)


@router.post("/{connection_id}/action", status_code=204)
async def connection_action(
    connection_id: uuid.UUID,
    payload: ConnectionActionRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Activa/desactiva, marca como principal o elimina una conexión."""
    # Verifica pertenencia al tenant activo antes de tocar nada.
    from core.db.models import ChannelConnection

    async with get_session() as session:
        existing = await get_owned(session, ChannelConnection, connection_id, principal.tenant_id)
    if existing is None:
        raise ApiError.not_found("Conexión no encontrada.")

    from core.gateway.connections import delete_connection, set_active, set_primary

    if payload.action == "activate":
        await set_active(connection_id, True)
    elif payload.action == "deactivate":
        await set_active(connection_id, False)
    elif payload.action == "primary":
        await set_primary(connection_id)
    else:
        await delete_connection(connection_id)
    return Response(status_code=204)

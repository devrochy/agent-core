"""Conexiones (lectura): `GET /api/v1/admin/connections` y la consola
`GET /api/v1/admin/connections/console` (HU-AOS-23).

Canales conectados del tenant: conexiones manuales (`channel_connections`) y las
de Meta (Embedded Signup / BYOK). Mismos datos que la vista SSR `/admin/conexiones`.
Requiere la sección `connections`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from core.admin.permissions import effective_access
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.forms import coerce_schema_values, redact_secrets
from core.db.models import AuditLog, PhoneNumber, utcnow
from core.db.session import get_owned, get_session
from core.gateway.connection_types import WEBCHAT, setup_url
from core.gateway.connections import administrada_aparte, list_connections
from core.shared.logger import get_logger
from core.shared.tenant import tenant_slug

log = get_logger(__name__)

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
                    phones.setdefault(p.connection_id, p.display_phone_number or p.phone_number_id)

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


# ------------------------------------------------------------------ consola (HU-AOS-23)
class ConsoleAgent(BaseModel):
    instance_id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    name: str
    role: Literal["core", "specialist", "backoffice"]
    is_default: bool
    enabled: bool
    priority: int
    release_version: int | None = Field(
        description="Versión que corre en la release activa; null si no figura."
    )
    latest_version: int | None = Field(description="Última versión publicada del agente.")


class ConsoleRelease(BaseModel):
    id: uuid.UUID
    number: int
    created_at: datetime


class ConsoleUsage(BaseModel):
    turns: int
    conversations: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = Field(description="Solo con acceso a la sección `costs`; si no, null.")


class ConsoleConnection(BaseModel):
    id: uuid.UUID
    channel: str
    type_id: str | None
    label: str
    external_id: str
    conversational: bool
    active: bool
    is_primary: bool
    created_at: datetime | None
    meta_connection_id: uuid.UUID | None
    health: Literal["ok", "degraded", "error", "paused", "pending"]
    pause_scope: Literal["meta_account", "connection", "webchat_settings"]
    pausable: bool = Field(
        description=(
            "La consola ofrece pausar: la cuenta de Meta, las conexiones manuales o de WhatsApp Web "
            "(ADR-0122 y su adenda 2026-10-19) y el chat web (alcance `connection`: el mismo estado que "
            "Configuración → Chat web). `webchat_settings` ya no se emite."
        )
    )
    coordinator: ConsoleAgent | None
    agents: list[ConsoleAgent] = Field(
        description="Especialistas y back-office, sin el Coordinador."
    )
    active_release: ConsoleRelease | None
    canary_release: ConsoleRelease | None
    usage_7d: ConsoleUsage


class ConsoleConsistency(BaseModel):
    pass_k: float
    k: int
    measured_at: datetime


class ConsoleUnassignedAgent(BaseModel):
    id: uuid.UUID
    key: str
    name: str


class ConnectionsConsole(BaseModel):
    """Con la sección `connections` basta para el consumo (turnos y tokens), la consistencia del
    negocio y los agentes de cada conexión: es lo que pide la consola (SPEC-26 §5.1,
    `conexiones:ver`). El costo en dinero sigue en `costs`."""

    items: list[ConsoleConnection]
    total: int
    page: int
    page_size: int
    has_more: bool
    runtime: Literal["legacy", "v3"]
    routing: Literal["off", "shadow", "active"] = Field(
        description="`tenants.aos_routing`, solo lectura."
    )
    consistency: ConsoleConsistency | None = Field(description="pass^k del último run del negocio.")
    can_edit: bool = Field(description="Acceso de escritura a `connections`.")
    unassigned_agents: list[ConsoleUnassignedAgent] = Field(
        description="D-35: agentes publicados sin ninguna conexión (máx. 50)."
    )


def _console_agent(a: Any) -> ConsoleAgent:
    return ConsoleAgent(
        instance_id=a.instance_id,
        agent_id=a.agent_id,
        key=a.key,
        name=a.name,
        role=a.role,
        is_default=a.is_default,
        enabled=a.enabled,
        priority=a.priority,
        release_version=a.release_version,
        latest_version=a.latest_version,
    )


def _console_release(r: Any) -> ConsoleRelease | None:
    return None if r is None else ConsoleRelease(id=r.id, number=r.number, created_at=r.created_at)


@router.get("/console", response_model=ConnectionsConsole)
async def connections_console(
    principal: Annotated[Principal, Depends(require("connections"))],
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConnectionsConsole:
    """Consola de conexiones (HU-AOS-23): por cada conexión del negocio, su Coordinador y
    agentes con la versión de la release activa, salud y consumo de 7 días. Solo lectura."""
    from core.gateway import consola_conexiones as svc

    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    tid = principal.tenant_id
    datos = await svc.consola(tid, offset=(page - 1) * page_size, limite=page_size)
    ve_costos = await effective_access(principal.admin, "costs", tid) is not None
    can_edit = await effective_access(principal.admin, "connections", tid) == "write"

    items: list[ConsoleConnection] = []
    for c in datos.items:
        core = next((a for a in c.agents if a.role == "core"), None)
        items.append(
            ConsoleConnection(
                id=c.id,
                channel=c.channel,
                type_id=c.type_id,
                label=c.label,
                external_id=c.external_id,
                conversational=c.conversational,
                active=c.active,
                is_primary=c.is_primary,
                created_at=c.created_at,
                meta_connection_id=c.meta_connection_id,
                health=c.health,  # type: ignore[arg-type]
                pause_scope=c.pause_scope,  # type: ignore[arg-type]
                pausable=c.pausable,
                coordinator=_console_agent(core) if core is not None else None,
                agents=[_console_agent(a) for a in c.agents if a.role != "core"],
                active_release=_console_release(c.active_release),
                canary_release=_console_release(c.canary_release),
                usage_7d=ConsoleUsage(
                    turns=c.usage_7d.turns,
                    conversations=c.usage_7d.conversations,
                    input_tokens=c.usage_7d.input_tokens,
                    output_tokens=c.usage_7d.output_tokens,
                    cost_usd=round(c.usage_7d.cost_usd, 4) if ve_costos else None,
                ),
            )
        )
    return ConnectionsConsole(
        items=items,
        total=datos.total,
        page=page,
        page_size=page_size,
        has_more=page * page_size < datos.total,
        runtime="v3" if datos.runtime == "v3" else "legacy",
        routing=datos.routing if datos.routing in ("off", "shadow", "active") else "off",  # type: ignore[arg-type]
        consistency=(
            ConsoleConsistency(
                pass_k=datos.consistency.pass_k,
                k=datos.consistency.k,
                measured_at=datos.consistency.measured_at,
            )
            if datos.consistency is not None
            else None
        ),
        can_edit=can_edit,
        unassigned_agents=[
            ConsoleUnassignedAgent(id=a.id, key=a.key, name=a.name) for a in datos.unassigned_agents
        ],
    )


async def audit_connection_action(principal: Principal, action: str, connection_id: str) -> None:
    """Rastro de una acción sobre una conexión (HU-AOS-23: la pausa queda auditada). Va después
    del cambio, en su propia transacción: ver «Decisiones revisables» del PR."""
    if principal.tenant_id is None:
        return
    try:
        async with get_session() as session:
            session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action=action,
                    input={"connection_id": str(uuid.UUID(str(connection_id)))},
                )
            )
    except Exception:
        # La acción ya se aplicó: un 500 invitaría a repetirla. Queda en el log para revisar.
        log.exception(
            "connection_action_audit_failed", action=action, connection_id=str(connection_id)
        )


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
    # Solo para `delete`: la contraseña actual del usuario (step-up).
    password: str | None = None


class CredentialField(BaseModel):
    name: str
    label: str
    value: str | None = None
    secret: bool = False
    configured: bool = False
    # `platform`: lo custodia la plataforma y no se revela al negocio (App Secret/verify de Meta).
    platform: bool = False


class ConnectionCredentialsResponse(BaseModel):
    id: uuid.UUID
    kind: Literal["manual"]
    type_id: str
    label: str
    fields: list[CredentialField]


class RevealCredentialsRequest(BaseModel):
    password: str = ""


class RevealedSecretsResponse(BaseModel):
    secrets: dict[str, str]


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
            existing = await get_owned(
                session, ChannelConnection, connection_id, principal.tenant_id
            )
        # La conexión canónica de un número de Meta no es manual (ADR-0115): no se edita acá.
        if existing is None or administrada_aparte(existing):
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


def _label_de_campo(name: str, prop: dict[str, Any]) -> str:
    title = prop.get("title")
    return str(title) if isinstance(title, str) and title else name.replace("_", " ").capitalize()


def _valor_visible(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "Sí" if value else "No"
    return str(value)


async def _manual_connection_of(principal: Principal, connection_id: uuid.UUID):
    """Conexión manual del tenant activo; las de Meta/chat web no se consultan por aquí."""
    from core.db.models import ChannelConnection

    async with get_session() as session:
        existing = await get_owned(session, ChannelConnection, connection_id, principal.tenant_id)
    if existing is None or administrada_aparte(existing):
        raise ApiError.not_found("Conexión no encontrada.")
    return existing


@router.get("/{connection_id}/credentials", response_model=ConnectionCredentialsResponse)
async def connection_credentials(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ConnectionCredentialsResponse:
    """Datos de registro de una conexión manual; los secretos solo indican si están cargados."""
    from core.api.forms import is_secret
    from core.gateway.connection_types import get_connection_type
    from core.gateway.connections import credentials_of

    existing = await _manual_connection_of(principal, connection_id)
    ctype = get_connection_type(existing.type_id) if existing.type_id else None
    if ctype is None:
        raise ApiError.not_found("Tipo de conexión no encontrado.")
    values = credentials_of(existing)
    fields: list[CredentialField] = []
    for name, prop in (ctype.credentials_schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        secreto = is_secret(prop)
        fields.append(
            CredentialField(
                name=name,
                label=_label_de_campo(name, prop),
                value=None if secreto else _valor_visible(values.get(name)),
                secret=secreto,
                configured=values.get(name) not in (None, ""),
            )
        )
    return ConnectionCredentialsResponse(
        id=existing.id,
        kind="manual",
        type_id=ctype.id,
        label=existing.label,
        fields=fields,
    )


@router.post("/{connection_id}/credentials/reveal", response_model=RevealedSecretsResponse)
async def reveal_connection_credentials(
    connection_id: uuid.UUID,
    payload: RevealCredentialsRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> RevealedSecretsResponse:
    """Revela los secretos de una conexión manual tras confirmar la contraseña del usuario."""
    from core.api.forms import is_secret
    from core.api.step_up import confirmar_password
    from core.gateway.connection_types import get_connection_type
    from core.gateway.connections import credentials_of

    await confirmar_password(principal, payload.password, proposito="connection_credentials")
    existing = await _manual_connection_of(principal, connection_id)
    ctype = get_connection_type(existing.type_id) if existing.type_id else None
    if ctype is None:
        raise ApiError.not_found("Tipo de conexión no encontrado.")
    values = credentials_of(existing)
    secrets = {
        name: str(values[name])
        for name, prop in (ctype.credentials_schema.get("properties") or {}).items()
        if isinstance(prop, dict) and is_secret(prop) and values.get(name) not in (None, "")
    }
    await audit_connection_action(principal, "connection.credentials_revealed", str(existing.id))
    return RevealedSecretsResponse(secrets=secrets)


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
        if existing is None or administrada_aparte(existing):
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
    except ValueError as exc:
        # Errores de negocio con texto pensado para el usuario.
        raise ApiError.bad_request(
            f"No se pudo guardar la conexión: {str(exc)[:140]}", "save_failed"
        ) from exc
    except Exception as exc:
        # Un error de la base no se muestra (nombres de constraints y valores).
        log.exception("connection_save_failed")
        raise ApiError.bad_request("No se pudo guardar la conexión.", "save_failed") from exc
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
    if (
        existing is not None
        and existing.type_id == WEBCHAT
        and existing.meta_connection_id is None
        and payload.action in ("activate", "deactivate")
    ):
        await _pausar_chat_web(principal, connection_id, enabled=payload.action == "activate")
        return Response(status_code=204)
    # La conexión canónica de un número de Meta no es manual (ADR-0115): sus acciones son las
    # de su cuenta (`/admin/meta/connections`). El chat web solo se pausa o reanuda aquí: su
    # clave y sus dominios están en Configuración → Chat web.
    if existing is None or administrada_aparte(existing):
        raise ApiError.not_found("Conexión no encontrada.")

    if payload.action == "delete":
        from core.api.step_up import confirmar_password

        await confirmar_password(principal, payload.password, proposito="connection_delete")

    from core.gateway.connections import delete_connection, set_active, set_primary

    if payload.action == "activate":
        await set_active(connection_id, True)
    elif payload.action == "deactivate":
        await set_active(connection_id, False)
    elif payload.action == "primary":
        await set_primary(connection_id)
    else:
        await delete_connection(connection_id)
    await audit_connection_action(principal, _ACCION_AUDITADA[payload.action], str(connection_id))
    return Response(status_code=204)


async def _pausar_chat_web(
    principal: Principal, connection_id: uuid.UUID, *, enabled: bool
) -> None:
    """El chat web desde la consola (HU-AOS-23): el mismo interruptor que Configuración → Chat web
    (`app_settings.web_chat_config.enabled` y `channel_connections.active` en una transacción) y
    la misma auditoría que las demás conexiones (también si ya estaba así, como ellas)."""
    from core.gateway import chat_web_conexion

    if principal.tenant_id is None:
        raise ApiError.not_found("Conexión no encontrada.")
    try:
        async with get_session() as session:
            previo = await chat_web_conexion.establecer_habilitado(
                session, principal.tenant_id, connection_id, enabled=enabled
            )
    except chat_web_conexion.WidgetDesincronizado:
        raise ApiError(
            409,
            "conflict",
            "No se pudo cambiar el chat web: revisa Configuración → Chat web y vuelve a intentarlo.",
        ) from None
    if previo is None:
        raise ApiError.not_found("Conexión no encontrada.")
    await audit_connection_action(
        principal, _ACCION_AUDITADA["activate" if enabled else "deactivate"], str(connection_id)
    )


_ACCION_AUDITADA = {
    "activate": "connection.resumed",
    "deactivate": "connection.paused",
    "primary": "connection.primary",
    "delete": "connection.deleted",
}

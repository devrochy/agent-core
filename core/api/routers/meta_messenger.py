"""Alta de Messenger en la API (ADR-0152, HU-CHAN-01): intento, descubrimiento y conexión de Páginas.

Delgado a propósito: valida la entrada y traduce los errores; la lógica (intento de un solo uso, canje,
reclamo de la Página, conexión canónica, suscripción verificada) vive en `core.gateway.meta_messenger_alta`.
Conectar una Página deja a un negocio atendiendo clientes reales, así que descubrir y conectar son del
propietario o del master, como el alta de producción de WhatsApp (ADR-0127).
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr

from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.config import get_settings
from core.gateway import meta_messenger_alta as alta

router = APIRouter(prefix="/admin/meta/messenger", tags=["admin · messenger"])

# Los IDs de Página son numéricos en Graph y van en la ruta de las llamadas: sin `/` ni `?` que la cambien.
_ID_DE_PAGINA = re.compile(r"[0-9]{1,32}")
MAX_PAGINAS = 50


class MessengerState(BaseModel):
    """Qué puede ofrecer el alta en este entorno. `app_id` y `config_id` son públicos (los usa el SDK de
    Facebook en el navegador); el App Secret nunca sale del servidor. `plan_includes`: el plan del negocio
    incluye el canal."""

    meta_configured: bool
    app_id: str
    config_id: str
    graph_api_version: str
    missing_settings: list[str] = []
    plan_includes: bool


class AttemptOut(BaseModel):
    """Valor de un solo uso que el panel devuelve con el `code` del popup."""

    attempt: str
    expires_in: int


class DiscoverIn(BaseModel):
    # `SecretStr`: no salen en el repr ni en los logs de validación.
    code: SecretStr
    attempt: SecretStr


class PageOut(BaseModel):
    page_id: str
    name: str
    category: str | None = None
    eligible: bool
    reason: str | None = None
    already_connected: bool = False


class DiscoverOut(BaseModel):
    connection_id: uuid.UUID
    pages: list[PageOut]


class ConnectIn(BaseModel):
    connection_id: uuid.UUID
    page_ids: Annotated[list[str], Field(min_length=1, max_length=MAX_PAGINAS)]


class ConnectedPageOut(BaseModel):
    page_id: str
    channel_connection_id: uuid.UUID
    subscribed: bool
    subscribed_fields: list[str] = []


class SkippedPageOut(BaseModel):
    page_id: str
    code: str
    message: str


class ConnectOut(BaseModel):
    """`connected` incluye las Páginas guardadas aunque Meta no haya confirmado la suscripción
    (`subscribed = false`): no reciben mensajes hasta reintentar. `skipped`: no se conectaron."""

    connection_id: uuid.UUID
    connected: list[ConnectedPageOut]
    skipped: list[SkippedPageOut]


def _traducir(exc: alta.MessengerAltaError) -> ApiError:
    if exc.status == 404:
        return ApiError.not_found(exc.message)
    return ApiError(exc.status, exc.code, exc.message)


def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Elige un negocio.", "tenant_required")
    return principal.tenant_id


@router.get("", response_model=MessengerState)
async def messenger_state(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> MessengerState:
    """Si el alta puede completarse aquí (app de Meta configurada y plan que incluye Messenger)."""
    from core.billing.enforcement import subscription_overview

    tenant_id = _negocio(principal)
    settings = get_settings()
    faltan = alta.faltan_para_messenger(settings)
    resumen = await subscription_overview(tenant_id)
    return MessengerState(
        meta_configured=not faltan,
        app_id=settings.meta_app_id or "",
        config_id=alta.config_id_de_messenger(settings),
        graph_api_version=settings.graph_api_version,
        missing_settings=faltan,
        plan_includes=alta.plan_incluye_messenger(resumen.plan_id),
    )


@router.post("/attempt", response_model=AttemptOut, status_code=201)
async def messenger_attempt(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> AttemptOut:
    """Abre un intento de popup de Facebook (un solo uso, vence a los 15 minutos)."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    try:
        await alta.exigir_plan(tenant_id)
    except alta.MessengerAltaError as exc:
        raise _traducir(exc) from exc
    return AttemptOut(
        attempt=await alta.iniciar_intento(tenant_id, principal.admin.id), expires_in=alta.INTENTO_TTL_S
    )


@router.post("/discover", response_model=DiscoverOut)
async def messenger_discover(
    payload: DiscoverIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> DiscoverOut:
    """Canjea el `code` del popup y lista las Páginas que el usuario administra, con cuáles pueden conectarse.
    Los tokens de las Páginas no salen del servidor."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    try:
        descubrimiento = await alta.descubrir_paginas(
            tenant_id, principal.admin.id, payload.code.get_secret_value().strip(),
            payload.attempt.get_secret_value().strip(),
        )
    except alta.MessengerAltaError as exc:
        raise _traducir(exc) from exc
    return DiscoverOut(
        connection_id=descubrimiento.connection_id,
        pages=[
            PageOut(
                page_id=p.page_id, name=p.name, category=p.category, eligible=p.elegible, reason=p.motivo,
                already_connected=p.page_id in descubrimiento.ya_conectadas,
            )
            for p in descubrimiento.paginas
        ],
    )


@router.post("/connect", response_model=ConnectOut)
async def messenger_connect(
    payload: ConnectIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConnectOut:
    """Conecta las Páginas elegidas: reclama cada una (un solo dueño), guarda su token, crea su conexión y
    suscribe y verifica el webhook. Una Página que falla no impide las demás: va en `skipped`."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    invalidas = [p for p in payload.page_ids if not _ID_DE_PAGINA.fullmatch(p.strip())]
    if invalidas:
        raise ApiError.bad_request("Un ID de Página no tiene un formato válido.", "invalid_id")
    try:
        resultado = await alta.conectar_paginas(tenant_id, principal.admin.id, payload.connection_id, payload.page_ids)
    except alta.MessengerAltaError as exc:
        raise _traducir(exc) from exc
    return ConnectOut(
        connection_id=resultado.connection_id,
        connected=[
            ConnectedPageOut(
                page_id=p.page_id, channel_connection_id=p.channel_connection_id,
                subscribed=p.suscrita, subscribed_fields=list(p.campos),
            )
            for p in resultado.conectadas
        ],
        skipped=[SkippedPageOut(page_id=o.page_id, code=o.code, message=o.motivo) for o in resultado.omitidas],
    )

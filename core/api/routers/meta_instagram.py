"""Alta de Instagram en la API (ADR-0152, HU-CHAN-04): intento, descubrimiento y conexión de cuentas.

Delgado a propósito: valida la entrada y traduce los errores; la lógica (intento de un solo uso, canje,
reclamo de la cuenta, conexión canónica, suscripción verificada) vive en `core.gateway.meta_instagram_alta`.
Conectar una cuenta deja a un negocio atendiendo clientes reales, así que descubrir y conectar son del
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
from core.gateway import meta_instagram_alta as alta

router = APIRouter(prefix="/admin/meta/instagram", tags=["admin · instagram"])

# Los IDs de cuenta de Instagram son numéricos en Graph y van en consultas: sin `/` ni `?` que las cambien.
_ID_DE_CUENTA = re.compile(r"\d{1,32}")
MAX_CUENTAS = 50


class InstagramState(BaseModel):
    """Qué puede ofrecer el alta en este entorno. `app_id` y `config_id` son públicos (los usa el SDK de
    Facebook en el navegador); el App Secret nunca sale del servidor. `plan_includes`: el plan del negocio
    incluye el canal."""

    meta_configured: bool
    app_id: str
    config_id: str
    graph_api_version: str
    missing_settings: list[str] = []
    plan_includes: bool


class InstagramAttemptOut(BaseModel):
    """Valor de un solo uso que el panel devuelve con el `code` del popup."""

    attempt: str
    expires_in: int


class InstagramDiscoverIn(BaseModel):
    # `SecretStr`: no salen en el repr ni en los logs de validación.
    code: SecretStr
    attempt: SecretStr


class InstagramAccountOut(BaseModel):
    ig_user_id: str
    username: str | None = None
    page_id: str
    page_name: str
    eligible: bool
    reason: str | None = None
    already_connected: bool = False


class InstagramDiscoverOut(BaseModel):
    connection_id: uuid.UUID
    accounts: list[InstagramAccountOut]
    pages_without_instagram: int


class InstagramConnectIn(BaseModel):
    connection_id: uuid.UUID
    ig_user_ids: Annotated[list[str], Field(min_length=1, max_length=MAX_CUENTAS)]


class InstagramConnectedOut(BaseModel):
    ig_user_id: str
    channel_connection_id: uuid.UUID
    subscribed: bool
    subscribed_fields: list[str] = []


class InstagramSkippedOut(BaseModel):
    ig_user_id: str
    code: str
    message: str


class InstagramConnectOut(BaseModel):
    """`connected` incluye las cuentas guardadas aunque Meta no haya confirmado la suscripción
    (`subscribed = false`): no reciben mensajes hasta reintentar. `skipped`: no se conectaron."""

    connection_id: uuid.UUID
    connected: list[InstagramConnectedOut]
    skipped: list[InstagramSkippedOut]


def _traducir(exc: alta.InstagramAltaError) -> ApiError:
    if exc.status == 404:
        return ApiError.not_found(exc.message)
    return ApiError(exc.status, exc.code, exc.message)


def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Elige un negocio.", "tenant_required")
    return principal.tenant_id


@router.get("", response_model=InstagramState)
async def instagram_state(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> InstagramState:
    """Si el alta puede completarse aquí (app de Meta configurada y plan que incluye Instagram)."""
    from core.billing.enforcement import subscription_overview

    tenant_id = _negocio(principal)
    settings = get_settings()
    faltan = alta.faltan_para_instagram(settings)
    resumen = await subscription_overview(tenant_id)
    return InstagramState(
        meta_configured=not faltan,
        app_id=settings.meta_app_id or "",
        config_id=alta.config_id_de_instagram(settings),
        graph_api_version=settings.graph_api_version,
        missing_settings=faltan,
        plan_includes=alta.plan_incluye_instagram(resumen.plan_id),
    )


@router.post("/attempt", response_model=InstagramAttemptOut, status_code=201)
async def instagram_attempt(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> InstagramAttemptOut:
    """Abre un intento de popup de Facebook (un solo uso, vence a los 15 minutos)."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    try:
        await alta.exigir_plan(tenant_id)
    except alta.InstagramAltaError as exc:
        raise _traducir(exc) from exc
    return InstagramAttemptOut(
        attempt=await alta.iniciar_intento(tenant_id, principal.admin.id), expires_in=alta.INTENTO_TTL_S
    )


@router.post("/discover", response_model=InstagramDiscoverOut)
async def instagram_discover(
    payload: InstagramDiscoverIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> InstagramDiscoverOut:
    """Canjea el `code` del popup y lista las cuentas de Instagram vinculadas a las Páginas que el usuario
    administra, con cuáles pueden conectarse. Los tokens de las Páginas no salen del servidor."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    try:
        descubrimiento = await alta.descubrir_cuentas(
            tenant_id, principal.admin.id, payload.code.get_secret_value().strip(),
            payload.attempt.get_secret_value().strip(),
        )
    except alta.InstagramAltaError as exc:
        raise _traducir(exc) from exc
    return InstagramDiscoverOut(
        connection_id=descubrimiento.connection_id,
        accounts=[
            InstagramAccountOut(
                ig_user_id=c.ig_user_id, username=c.username, page_id=c.page_id, page_name=c.page_name,
                eligible=c.elegible, reason=c.motivo, already_connected=c.ig_user_id in descubrimiento.ya_conectadas,
            )
            for c in descubrimiento.cuentas
        ],
        pages_without_instagram=descubrimiento.paginas_sin_instagram,
    )


@router.post("/connect", response_model=InstagramConnectOut)
async def instagram_connect(
    payload: InstagramConnectIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> InstagramConnectOut:
    """Conecta las cuentas elegidas: reclama cada una (un solo dueño), guarda el token de su Página vinculada,
    crea su conexión y suscribe y verifica el webhook. Una cuenta que falla no impide las demás: va en `skipped`."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    if any(not _ID_DE_CUENTA.fullmatch(i.strip()) for i in payload.ig_user_ids):
        raise ApiError.bad_request("Un ID de cuenta de Instagram no tiene un formato válido.", "invalid_id")
    try:
        resultado = await alta.conectar_cuentas(tenant_id, principal.admin.id, payload.connection_id, payload.ig_user_ids)
    except alta.InstagramAltaError as exc:
        raise _traducir(exc) from exc
    return InstagramConnectOut(
        connection_id=resultado.connection_id,
        connected=[
            InstagramConnectedOut(
                ig_user_id=c.ig_user_id, channel_connection_id=c.channel_connection_id,
                subscribed=c.suscrita, subscribed_fields=list(c.campos),
            )
            for c in resultado.conectadas
        ],
        skipped=[InstagramSkippedOut(ig_user_id=o.ig_user_id, code=o.code, message=o.motivo) for o in resultado.omitidas],
    )

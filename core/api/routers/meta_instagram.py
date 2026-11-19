"""Alta de Instagram en la API (ADR-0152, HU-CHAN-04): intento, descubrimiento y conexión de cuentas.

Delgado a propósito: valida la entrada y traduce los errores; la lógica (intento de un solo uso, canje,
reclamo de la cuenta, conexión canónica, suscripción verificada) vive en `core.gateway.meta_instagram_alta`.
Conectar una cuenta deja a un negocio atendiendo clientes reales, así que descubrir y conectar son del
propietario o del master, como el alta de producción de WhatsApp (ADR-0127).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr

from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.config import get_settings
from core.gateway import meta_instagram_alta as alta
from core.gateway import meta_instagram_login_alta as login_alta

router = APIRouter(prefix="/admin/meta/instagram", tags=["admin · instagram"])

# Los IDs de cuenta de Instagram son numéricos en Graph y van en consultas: sin `/` ni `?` que las cambien.
_ID_DE_CUENTA = re.compile(r"[0-9]{1,32}")
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
    # Instagram Login (ruta b): apagada por defecto. `login_missing` dice qué falta si no está disponible.
    login_available: bool = False
    login_missing: list[str] = []


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
        login_available=not (login_faltan := login_alta.login_disponible(settings)),
        login_missing=login_faltan,
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


# ------------------------------------------------------------------ Instagram Login (ruta b)
class InstagramLoginStartOut(BaseModel):
    """Dirección de autorización de Instagram a la que el panel manda el navegador."""

    url: str
    expires_in: int


class InstagramLoginCompleteIn(BaseModel):
    # `SecretStr`: no salen en el repr ni en los logs de validación.
    code: SecretStr
    state: SecretStr


class InstagramLoginCompleteOut(BaseModel):
    ig_user_id: str
    username: str | None = None
    channel_connection_id: uuid.UUID
    connection_id: uuid.UUID
    subscribed: bool
    subscribed_fields: list[str] = []


@router.post("/login/start", response_model=InstagramLoginStartOut, status_code=201)
async def instagram_login_start(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> InstagramLoginStartOut:
    """Arranca el alta con Instagram Login: el `state` es un intento de un solo uso atado al negocio y al admin."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    try:
        iniciado = await login_alta.iniciar_login(tenant_id, principal.admin.id)
    except alta.InstagramAltaError as exc:
        raise _traducir(exc) from exc
    return InstagramLoginStartOut(url=iniciado.url, expires_in=iniciado.expires_in)


@router.post("/login/complete", response_model=InstagramLoginCompleteOut)
async def instagram_login_complete(
    payload: InstagramLoginCompleteIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> InstagramLoginCompleteOut:
    """Canjea el `code` de Instagram y conecta la cuenta que la persona autorizó. Los tokens no salen del servidor."""
    require_owner_or_master(principal)
    tenant_id = _negocio(principal)
    try:
        cuenta = await login_alta.completar_login(
            tenant_id, principal.admin.id, payload.code.get_secret_value().strip(),
            payload.state.get_secret_value().strip(),
        )
    except alta.InstagramAltaError as exc:
        raise _traducir(exc) from exc
    return InstagramLoginCompleteOut(
        ig_user_id=cuenta.ig_user_id, username=cuenta.username, channel_connection_id=cuenta.channel_connection_id,
        connection_id=cuenta.connection_id, subscribed=cuenta.suscrita, subscribed_fields=list(cuenta.campos),
    )


# ------------------------------------------------------------------ cuentas conectadas y diagnóstico
DIAGNOSTICO_ESPERA_S = 90
# Tope de `since`: la consulta recorre el registro de entregas del negocio, que no tiene índice por cuenta.
DIAGNOSTICO_MAX_ANTIGUEDAD_H = 24


class InstagramAccountStatusOut(BaseModel):
    ig_user_id: str
    username: str | None = None
    login_route: str | None = None
    page_id: str | None = None
    status: str
    status_reason: str | None = None
    subscribed_fields: list[str] = []
    token_expires_at: datetime | None = None


class InstagramAccountsOut(BaseModel):
    accounts: list[InstagramAccountStatusOut]


class InstagramDiagnosticStartOut(BaseModel):
    """Momento (del servidor) desde el que se espera el mensaje de prueba, y cuánto vale esperar."""

    since: datetime
    wait_seconds: int


class InstagramDiagnosticOut(BaseModel):
    """`arrived`: llegó un mensaje de un cliente a esta cuenta desde `since`. Si no, `likely_cause` es lo más probable:
    `token_degraded` (reconectar), `not_subscribed` (la suscripción no confirmó `messages`) o
    `access_to_messages_off` (todo parece bien: casi siempre es «Permitir acceso a los mensajes» apagado en Instagram,
    que Meta nunca reporta como error)."""

    arrived: bool
    likely_cause: str | None = None


async def _cuenta_del_negocio(tenant_id: uuid.UUID, ig_user_id: str):
    from sqlalchemy import select

    from core.db.models import IgAccount, MetaConnection
    from core.db.session import get_session
    from core.gateway.meta_instagram_cuentas import no_reemplazada

    async with get_session() as session:
        fila = (
            await session.execute(
                select(IgAccount)
                .join(MetaConnection, MetaConnection.id == IgAccount.connection_id)
                .where(
                    IgAccount.tenant_id == tenant_id,
                    IgAccount.ig_user_id == ig_user_id,
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.deleted_at.is_(None),
                    no_reemplazada(),  # la ruta que dejó de usarse no cuenta
                )
                .order_by(IgAccount.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if fila is None:
        raise ApiError.not_found("No encontré esa cuenta de Instagram.")
    return fila


@router.get("/accounts", response_model=InstagramAccountsOut)
async def instagram_accounts(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> InstagramAccountsOut:
    """Las cuentas de Instagram del negocio con su estado (`connected` o `degraded`) y, en Instagram Login, cuándo vence
    su token. Sin tokens."""
    from sqlalchemy import select

    from core.db.models import IgAccount, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_instagram import LOGIN_ROUTE_INSTAGRAM, estado_de
    from core.gateway.meta_instagram_cuentas import no_reemplazada
    from core.gateway.meta_instagram_login import IG_LONG_LIVED_TOKEN_KIND

    tenant_id = _negocio(principal)
    async with get_session() as session:
        filas = (
            await session.execute(
                select(IgAccount)
                .join(MetaConnection, MetaConnection.id == IgAccount.connection_id)
                .where(
                    IgAccount.tenant_id == tenant_id,
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.deleted_at.is_(None),
                    no_reemplazada(),
                )
                .order_by(IgAccount.created_at)
            )
        ).scalars().all()
    cuentas: list[InstagramAccountStatusOut] = []
    for fila in filas:
        estado, motivo = estado_de(fila.raw)
        vence = None
        if fila.login_route == LOGIN_ROUTE_INSTAGRAM:
            vence = await meta_token_store.get_token_expires_at(
                fila.connection_id, IG_LONG_LIVED_TOKEN_KIND, fila.ig_user_id, tenant_id=tenant_id
            )
        cuentas.append(
            InstagramAccountStatusOut(
                ig_user_id=fila.ig_user_id, username=fila.username, login_route=fila.login_route,
                page_id=fila.linked_page_id, status=estado, status_reason=motivo,
                subscribed_fields=list((fila.raw or {}).get("subscribed_fields") or []), token_expires_at=vence,
            )
        )
    return InstagramAccountsOut(accounts=cuentas)


@router.post("/accounts/{ig_user_id}/diagnostic", response_model=InstagramDiagnosticStartOut)
async def instagram_diagnostic_start(
    ig_user_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> InstagramDiagnosticStartOut:
    """Prueba de humo: la persona envía un mensaje directo a esta cuenta desde OTRO perfil de Instagram y el panel
    consulta si llegó. Devuelve el momento (del servidor) desde el que se cuenta."""
    if not _ID_DE_CUENTA.fullmatch(ig_user_id):
        raise ApiError.bad_request("El ID de la cuenta no tiene un formato válido.", "invalid_id")
    await _cuenta_del_negocio(_negocio(principal), ig_user_id)
    return InstagramDiagnosticStartOut(since=datetime.now(UTC), wait_seconds=DIAGNOSTICO_ESPERA_S)


@router.get("/accounts/{ig_user_id}/diagnostic", response_model=InstagramDiagnosticOut)
async def instagram_diagnostic(
    ig_user_id: str,
    since: datetime,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> InstagramDiagnosticOut:
    """¿Llegó al webhook un mensaje de un cliente a esta cuenta desde `since`? Se mira el registro de entregas del
    negocio (nunca el de otro). Si no llegó, dice la causa más probable."""
    from core.gateway.meta_instagram import ESTADO_DEGRADADA, estado_de
    from core.gateway.webhooks import mensajes_recibidos_desde

    if not _ID_DE_CUENTA.fullmatch(ig_user_id):
        raise ApiError.bad_request("El ID de la cuenta no tiene un formato válido.", "invalid_id")
    tenant_id = _negocio(principal)
    cuenta = await _cuenta_del_negocio(tenant_id, ig_user_id)
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    since = max(since, datetime.now(UTC) - timedelta(hours=DIAGNOSTICO_MAX_ANTIGUEDAD_H))
    llegaron = await mensajes_recibidos_desde(tenant_id, "instagram", ig_user_id, since)
    if llegaron:
        return InstagramDiagnosticOut(arrived=True)
    estado, _motivo = estado_de(cuenta.raw)
    campos = (cuenta.raw or {}).get("subscribed_fields") or []
    if estado == ESTADO_DEGRADADA:
        causa = "token_degraded"
    elif "messages" not in campos:
        causa = "not_subscribed"
    else:
        causa = "access_to_messages_off"
    return InstagramDiagnosticOut(arrived=False, likely_cause=causa)

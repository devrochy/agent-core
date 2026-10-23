"""Salud de conexiones Meta y suscripciones de webhook.

`GET /api/v1/admin/meta/health` y `/webhooks` (lectura) más las acciones de
reparación: diagnosticar, re-suscribir la WABA, marcar facturación y registrar la
suscripción de APP de un canal. Reusa `core.admin.meta_health_service` (fuente
única con el panel SSR). Requiere la sección `connections`; registrar exige
además ser `owner` del negocio o `master` (igual que el SSR).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, SecretStr
from sqlalchemy import select

from core.admin.meta_health_service import (
    WebhookRegistroError,
    diagnose_connection,
    health_overview,
    register_channel_subscription,
    register_connection_webhook,
    resubscribe_waba,
    set_billing_status,
    set_connection_status,
    soft_delete_connection,
    webhook_subscriptions_status,
)
from core.admin.meta_numbers_service import (
    add_number,
    business_numbers,
    list_numbers,
    register_number,
    request_code,
    verify_number,
)
from core.admin.meta_templates_service import (
    delete_template,
    list_templates,
    sync_templates_now,
)
from core.admin.roles import actua_como_master
from core.admin.waweb_service import (
    accept_risk as waweb_accept_risk,
)
from core.admin.waweb_service import (
    reset_session as waweb_reset_session,
)
from core.admin.waweb_service import (
    session_state as waweb_session_state,
)
from core.api.deps import Principal, es_owner_o_master, require, require_owner_or_master
from core.api.errors import ApiError
from core.api.schemas import ReadinessRun, readiness_run_out
from core.db.session import get_session
from core.gateway.meta_connection_state import get_live_meta_connection

if TYPE_CHECKING:
    from core.gateway.meta_credentials import MetaCredentials

router = APIRouter(prefix="/admin/meta", tags=["admin · meta"])


def _uuid_o_none(valor: str) -> uuid.UUID:
    """UUID de la ruta; uno inválido se trata como inexistente (404), nunca como 500."""
    try:
        return uuid.UUID(valor)
    except ValueError:
        return uuid.UUID(int=0)


class HealthCheck(BaseModel):
    """Un check del último diagnóstico. `ok`: `None` = no se pudo evaluar.
    `repair_action`: id corto que la UI traduce a un botón o a una ruta."""

    key: str
    ok: bool | None
    detail: str | None = None
    repair_action: str | None = None
    checked_at: datetime | None = None


class HealthConnection(BaseModel):
    id: uuid.UUID
    channel: str
    environment: str
    # Modo de credenciales (`byok` | `embedded_signup` | `manual`, ADR-0094): el panel ofrece
    # "Pasar a producción" a las BYOK de prueba (HU-CONN-06).
    origin: str = ""
    status: str
    status_reason: str | None
    last_health_check_at: datetime | None
    # `health` crudo: incluye datos del alta que no son checks. Para mostrar, `checks`.
    health: dict[str, Any]
    checks: list[HealthCheck] = []
    billing_manual_status: str | None = None
    business_id: str | None
    meta_app_id: str
    token_expires_at: datetime | None = None


class HealthResponse(BaseModel):
    connections: list[HealthConnection]


class BillingRequest(BaseModel):
    manual_status: str


class SubscriptionItem(BaseModel):
    """Suscripción de la APP que usa una conexión (HU-CONN-05). Con la app de la
    plataforma y sin ser master, `callback_url` y `last_error` vienen vacíos."""

    connection_id: uuid.UUID
    origin: str
    uses_platform_app: bool
    can_register: bool
    channel: str
    object: str
    meta_app_id: str
    callback_url: str | None
    active: bool | None
    fields: list[str]
    last_error: str | None


class WebhooksResponse(BaseModel):
    subscriptions: list[SubscriptionItem]


class RegisterRequest(BaseModel):
    """Ruta vieja: mejor `POST /connections/{id}/webhook/register`. Con `connection_id`
    registra esa conexión; con solo `channel`, la única conexión viva del canal."""

    channel: str = ""
    connection_id: uuid.UUID | None = None


_es_owner_o_master = es_owner_o_master
_require_owner_or_master = require_owner_or_master


def _api_error(exc: WebhookRegistroError) -> ApiError:
    if exc.status == 404:
        return ApiError.not_found(exc.message)
    if exc.status == 403:
        return ApiError(403, exc.code, exc.message)
    return ApiError.bad_request(exc.message, exc.code)


@router.get("/health", response_model=HealthResponse)
async def get_health(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> HealthResponse:
    rows = await health_overview(principal.tenant_id)
    return HealthResponse(connections=[HealthConnection(**row) for row in rows])


@router.post("/health/{connection_id}/diagnose", status_code=204)
async def diagnose(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await diagnose_connection(principal.tenant_id, connection_id)
    if error is not None:
        raise ApiError.bad_request(error, "diagnose_failed")
    return Response(status_code=204)


@router.post("/health/{connection_id}/resubscribe", status_code=204)
async def resubscribe(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await resubscribe_waba(principal.tenant_id, connection_id)
    if error is not None:
        raise ApiError.bad_request(error, "resubscribe_failed")
    return Response(status_code=204)


@router.post("/health/{connection_id}/billing", status_code=204)
async def billing(
    connection_id: str,
    payload: BillingRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await set_billing_status(principal.tenant_id, connection_id, payload.manual_status)
    if error is not None:
        raise ApiError.bad_request(error, "billing_failed")
    return Response(status_code=204)


@router.get("/webhooks", response_model=WebhooksResponse)
async def get_webhooks(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> WebhooksResponse:
    rows = await webhook_subscriptions_status(
        principal.tenant_id,
        es_master=actua_como_master(principal.admin),
        puede_registrar=_es_owner_o_master(principal),
    )
    return WebhooksResponse(subscriptions=[SubscriptionItem(**row) for row in rows])


@router.post("/connections/{connection_id}/webhook/register", status_code=204)
async def register_connection_webhook_route(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Registra la suscripción de la app de ESA conexión (HU-CONN-05): BYOK en su propia
    app (dueño o master); la app de la plataforma (Embedded Signup), solo el master."""
    _require_owner_or_master(principal)
    try:
        await register_connection_webhook(
            principal.tenant_id, connection_id, es_master=actua_como_master(principal.admin)
        )
    except WebhookRegistroError as exc:
        raise _api_error(exc) from exc
    return Response(status_code=204)


@router.post("/webhooks/register", status_code=204, deprecated=True)
async def register_webhook(
    payload: RegisterRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Obsoleta: usa `POST /connections/{connection_id}/webhook/register`."""
    _require_owner_or_master(principal)
    es_master = actua_como_master(principal.admin)
    try:
        if payload.connection_id is not None:
            await register_connection_webhook(
                principal.tenant_id, str(payload.connection_id), es_master=es_master
            )
        else:
            await register_channel_subscription(
                principal.tenant_id, payload.channel, es_master=es_master
            )
    except WebhookRegistroError as exc:
        raise _api_error(exc) from exc
    return Response(status_code=204)


# ------------------------------------------------------------------ números
class PhoneNumberItem(BaseModel):
    id: uuid.UUID
    phone_number_id: str
    display_phone_number: str | None
    verified_name: str | None
    status: str
    # Registrado en la Cloud API con PIN (`POST .../register`): sin esto el número no manda ni recibe.
    registered: bool = False


class NumbersResponse(BaseModel):
    numbers: list[PhoneNumberItem]
    sync_error: str | None = None


class BusinessNumberItem(BaseModel):
    waba_id: str
    waba_name: str | None = None
    # La WABA de esta conexión (las demás son del mismo portafolio de Meta).
    is_connection_waba: bool
    phone_number_id: str
    display_phone_number: str | None = None
    verified_name: str | None = None
    status: str | None = None
    code_verification_status: str | None = None
    platform_type: str | None = None
    quality_rating: str | None = None
    name_status: str | None = None
    # Ya está en los números de esta conexión en el panel.
    in_this_connection: bool


class BusinessNumbersResponse(BaseModel):
    business_id: str | None = None
    business_name: str | None = None
    numbers: list[BusinessNumberItem]
    # Números del portafolio que ya tiene otro negocio de la plataforma: cuentan para el límite
    # de Meta pero no se muestran.
    other_tenants_count: int = 0
    warnings: list[str] = []


class AddNumberRequest(BaseModel):
    cc: str
    phone_number: str
    verified_name: str


class RequestCodeRequest(BaseModel):
    code_method: str
    language: str = "es"


class VerifyCodeRequest(BaseModel):
    code: str


class RegisterNumberRequest(BaseModel):
    # PIN de verificación en dos pasos que ya tenga el número (6 dígitos). Sin él, se usa el
    # guardado o se genera. `SecretStr`: el repr del modelo (p. ej. en un frame que capture
    # Sentry) no lo muestra.
    pin: SecretStr | None = None


@router.get("/connections/{connection_id}/numbers", response_model=NumbersResponse)
async def list_connection_numbers(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> NumbersResponse:
    """Números de una conexión Meta (sincroniza con Meta antes de listar)."""
    rows, sync_error = await list_numbers(principal.tenant_id, connection_id)
    return NumbersResponse(numbers=[PhoneNumberItem(**row) for row in rows], sync_error=sync_error)


@router.get("/connections/{connection_id}/business-numbers", response_model=BusinessNumbersResponse)
async def list_business_numbers(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> BusinessNumbersResponse:
    """Números de todas las WABAs propias del portafolio de Meta de la conexión (el límite de
    números de Meta cuenta el portafolio entero). Solo lectura: no guarda nada."""
    data, error = await business_numbers(principal.tenant_id, connection_id)
    if error is not None or data is None:
        raise ApiError.bad_request(
            error or "No se pudieron leer los números", "business_numbers_failed"
        )
    return BusinessNumbersResponse(
        business_id=data["business_id"],
        business_name=data["business_name"],
        numbers=[BusinessNumberItem(**n) for n in data["numbers"]],
        other_tenants_count=data["other_tenants_count"],
        warnings=data["warnings"],
    )


@router.post("/connections/{connection_id}/numbers", status_code=204)
async def add_connection_number(
    connection_id: str,
    payload: AddNumberRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await add_number(
        principal.tenant_id,
        connection_id,
        cc=payload.cc,
        phone_number=payload.phone_number,
        verified_name=payload.verified_name,
    )
    if error is not None:
        raise ApiError.bad_request(error, "add_number_failed")
    return Response(status_code=204)


@router.post("/connections/{connection_id}/numbers/{phone_number_id}/request-code", status_code=204)
async def request_connection_code(
    connection_id: str,
    phone_number_id: str,
    payload: RequestCodeRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await request_code(
        principal.tenant_id,
        connection_id,
        phone_number_id,
        code_method=payload.code_method,
        language=payload.language,
    )
    if error is not None:
        raise ApiError.bad_request(error, "request_code_failed")
    return Response(status_code=204)


@router.post("/connections/{connection_id}/numbers/{phone_number_id}/verify", status_code=204)
async def verify_connection_number(
    connection_id: str,
    phone_number_id: str,
    payload: VerifyCodeRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    ok, error = await verify_number(
        principal.tenant_id, connection_id, phone_number_id, code=payload.code
    )
    if error is not None:
        raise ApiError.bad_request(error, "verify_failed")
    if not ok:
        raise ApiError.bad_request("Código incorrecto.", "wrong_code")
    return Response(status_code=204)


@router.post("/connections/{connection_id}/numbers/{phone_number_id}/register", status_code=204)
async def register_connection_number(
    connection_id: str,
    phone_number_id: str,
    payload: RegisterNumberRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Registra el número en la Cloud API con un PIN de verificación en dos pasos, para
    conexiones BYOK y manuales (Embedded Signup lo hace en su wizard). El PIN nunca vuelve."""
    error = await register_number(
        principal.tenant_id,
        connection_id,
        phone_number_id,
        pin=payload.pin.get_secret_value() if payload.pin is not None else None,
    )
    if error is not None:
        raise ApiError.bad_request(error, "register_failed")
    return Response(status_code=204)


# ------------------------------------------------------------------ plantillas
class TemplateItem(BaseModel):
    id: uuid.UUID
    name: str
    language: str
    category: str | None
    status: str
    quality_score: str | None
    rejected_reason: str | None


class TemplatesResponse(BaseModel):
    templates: list[TemplateItem]
    sync_error: str | None = None
    at_limit: bool = False


@router.get("/connections/{connection_id}/templates", response_model=TemplatesResponse)
async def list_connection_templates(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> TemplatesResponse:
    """Plantillas de la WABA (sincroniza antes de listar, con lock de 1 h)."""
    rows, sync_error, at_limit = await list_templates(principal.tenant_id, connection_id)
    return TemplatesResponse(
        templates=[TemplateItem(**row) for row in rows],
        sync_error=sync_error,
        at_limit=at_limit,
    )


@router.post("/connections/{connection_id}/templates/sync", status_code=204)
async def sync_connection_templates(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Sincroniza las plantillas con Meta ahora mismo (sin el candado horario del cron).

    429 `sync_rate_limited`: ya se sincronizó a mano hace menos de un minuto. 400 `sync_failed`:
    Meta rechazó la consulta (el mensaje trae el motivo)."""
    error = await sync_templates_now(principal.tenant_id, connection_id)
    if error is not None:
        if error.code == "rate_limited":
            raise ApiError(429, "sync_rate_limited", error.message)
        raise ApiError.bad_request(error.message, "sync_failed")
    return Response(status_code=204)


@router.post("/connections/{connection_id}/templates/{template_id}/delete", status_code=204)
async def delete_connection_template(
    connection_id: str,
    template_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Borra TODAS las versiones de idioma de la plantilla.

    409 `template_not_deletable`: Meta no permite borrarla (sus plantillas de ejemplo); la
    plantilla sigue como estaba. 404 `not_found` si no es de este negocio."""
    error = await delete_template(principal.tenant_id, connection_id, template_id)
    if error is not None:
        if error.code == "not_deletable":
            raise ApiError(409, "template_not_deletable", error.message)
        if error.code == "not_found":
            raise ApiError.not_found(error.message)
        raise ApiError.bad_request(error.message, "delete_failed")
    return Response(status_code=204)


# ------------------------------------------------------------------ WhatsApp Web (ADR-0074)
class WawebState(BaseModel):
    status: str
    risk_ok: bool
    jid: str | None = None
    qr_image: str | None = None
    last_error: str | None = None


@router.get("/waweb", response_model=WawebState)
async def get_waweb_state(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> WawebState:
    """Estado del vínculo por QR del canal WhatsApp Web (no oficial)."""
    state = await waweb_session_state(principal.tenant_id)
    return WawebState(**state)


@router.post("/waweb/accept-risk", status_code=204)
async def accept_waweb_risk(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await waweb_accept_risk(principal.tenant_id, principal.admin.id)
    if error is not None:
        raise ApiError.bad_request(error, "accept_risk_failed")
    return Response(status_code=204)


@router.post("/waweb/reset", status_code=204)
async def reset_waweb_session(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await waweb_reset_session(principal.tenant_id)
    if error is not None:
        raise ApiError.bad_request(error, "reset_failed")
    return Response(status_code=204)


class MetaConnectionAction(BaseModel):
    action: Literal["activate", "deactivate", "delete", "primary"]
    # Solo para `delete`: la contraseña actual del usuario (step-up).
    password: str | None = None


PRIMARY_NOT_APPLICABLE = (
    "Las conexiones de Meta no se marcan como principal: el asistente responde por el número por el "
    "que escribió cada cliente y usa las credenciales de su propia cuenta de Meta. «Principal» solo "
    "aplica a las conexiones manuales (credenciales cargadas a mano), como respaldo del envío."
)


@router.post("/connections/{connection_id}/action", status_code=204)
async def meta_connection_action(
    connection_id: str,
    payload: MetaConnectionAction,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Activa, desactiva o elimina (soft-delete) una conexión de Meta.

    `primary` responde 409 `primary_not_applicable` con la explicación: la primacía es el respaldo
    de credenciales de las conexiones manuales (ADR-0069); una cuenta de Meta resuelve las suyas y su
    conexión canónica nace y queda con `is_primary = false` (ADR-0115 §1)."""
    if payload.action == "primary":
        async with get_session() as session:
            existe = await get_live_meta_connection(
                session, _uuid_o_none(connection_id), principal.tenant_id
            )
        if existe is None:
            raise ApiError.not_found("Conexión no encontrada.")
        raise ApiError(409, "primary_not_applicable", PRIMARY_NOT_APPLICABLE)
    if payload.action == "delete":
        from core.api.step_up import confirmar_password

        await confirmar_password(principal, payload.password, proposito="meta_connection_delete")
        error = await soft_delete_connection(principal.tenant_id, connection_id)
    else:
        error = await set_connection_status(
            principal.tenant_id, connection_id, active=payload.action == "activate"
        )
    if error is not None:
        raise ApiError.bad_request(error, "meta_connection_failed")
    from core.api.routers.connections import audit_connection_action

    await audit_connection_action(
        principal,
        {
            "activate": "meta_connection.resumed",
            "deactivate": "meta_connection.paused",
            "delete": "meta_connection.deleted",
        }[payload.action],
        connection_id,
    )
    return Response(status_code=204)


class MetaCredentialField(BaseModel):
    name: str
    label: str
    value: str | None = None
    secret: bool = False
    configured: bool = False
    # `platform`: secreto de la app de la plataforma; nunca se revela al negocio.
    platform: bool = False


class MetaCredentialsResponse(BaseModel):
    id: uuid.UUID
    kind: Literal["meta"]
    channel: str
    environment: str
    origin: str
    fields: list[MetaCredentialField]


class MetaRevealRequest(BaseModel):
    password: str = ""


class MetaRevealedSecrets(BaseModel):
    secrets: dict[str, str]


async def _meta_connection_of(principal: Principal, connection_id: str):
    async with get_session() as session:
        return await get_live_meta_connection(
            session, _uuid_o_none(connection_id), principal.tenant_id
        )


async def _meta_datos_de_la_conexion(
    principal: Principal, connection
) -> tuple[list[MetaCredentialField], Any]:
    """Campos visibles (sin secretos) + credenciales efectivas para decidir qué se revela."""
    from core.db.models import PhoneNumber, WabaAccount
    from core.gateway.meta_credentials import (
        ORIGIN_LABELS,
        CredentialsUnavailable,
        access_token_expires_at,
        credentials_for,
        stored_verify_token,
        uses_platform_app,
    )

    creds = None
    try:
        creds = await credentials_for(connection)
    except CredentialsUnavailable:
        creds = None

    async with get_session() as session:
        wabas = (
            (
                await session.execute(
                    select(WabaAccount.waba_id).where(
                        WabaAccount.connection_id == connection.id,
                        WabaAccount.tenant_id == principal.tenant_id,
                        WabaAccount.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        numeros = (
            await session.execute(
                select(PhoneNumber.display_phone_number, PhoneNumber.phone_number_id).where(
                    PhoneNumber.connection_id == connection.id,
                    PhoneNumber.tenant_id == principal.tenant_id,
                    PhoneNumber.deleted_at.is_(None),
                )
            )
        ).all()

    de_plataforma = uses_platform_app(connection)
    verify_token = None if de_plataforma else await stored_verify_token(connection)
    expira = await access_token_expires_at(connection)

    fields = [
        MetaCredentialField(
            name="app_id", label="App ID", value=(creds.app_id if creds else connection.meta_app_id)
        ),
        MetaCredentialField(
            name="business_id",
            label="Business ID",
            value=(creds.business_id if creds else connection.business_id),
        ),
        MetaCredentialField(
            name="waba_id",
            label="WABA ID",
            value=", ".join(str(w) for w in wabas) or (creds.waba_id if creds else None),
        ),
        MetaCredentialField(
            name="numeros",
            label="Números",
            value=", ".join(str(n[0] or n[1]) for n in numeros) or None,
        ),
        MetaCredentialField(
            name="entorno",
            label="Entorno",
            value="Producción" if connection.environment == "production" else "Prueba",
        ),
        MetaCredentialField(
            name="origen",
            label="Origen",
            value=ORIGIN_LABELS.get(connection.origin, connection.origin),
        ),
        MetaCredentialField(
            name="graph_version", label="Versión de Graph", value=connection.graph_api_version
        ),
        MetaCredentialField(
            name="token_expira",
            label="El token vence",
            value=expira.isoformat() if expira else None,
        ),
        MetaCredentialField(
            name="access_token",
            label="Access token",
            secret=True,
            configured=bool(creds and creds.access_token),
        ),
        MetaCredentialField(
            name="app_secret",
            label="App Secret",
            secret=True,
            configured=bool(creds and creds.app_secret),
            platform=de_plataforma,
        ),
        MetaCredentialField(
            name="verify_token",
            label="Verify token",
            secret=True,
            configured=bool(verify_token),
            platform=de_plataforma,
        ),
    ]
    return fields, (creds, verify_token)


@router.get("/connections/{connection_id}/credentials", response_model=MetaCredentialsResponse)
async def meta_connection_credentials(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
    response: Response,
) -> MetaCredentialsResponse:
    """Datos de registro de una conexión Meta; los secretos solo indican si están cargados."""
    connection = await _meta_connection_of(principal, connection_id)
    if connection is None:
        raise ApiError.not_found("Conexión no encontrada.")
    fields, _ = await _meta_datos_de_la_conexion(principal, connection)
    response.headers["Cache-Control"] = "no-store"
    return MetaCredentialsResponse(
        id=connection.id,
        kind="meta",
        channel=connection.channel,
        environment=connection.environment,
        origin=connection.origin,
        fields=fields,
    )


@router.post("/connections/{connection_id}/credentials/reveal", response_model=MetaRevealedSecrets)
async def reveal_meta_connection_credentials(
    connection_id: str,
    payload: MetaRevealRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
    response: Response,
) -> MetaRevealedSecrets:
    """Revela los secretos propios del negocio tras confirmar la contraseña.

    Nunca devuelve el App Secret ni el verify token de la **app de la plataforma**
    (Embedded Signup / carga manual): esos son compartidos y no pertenecen al negocio.
    """
    from core.api.step_up import confirmar_password
    from core.gateway.meta_credentials import (
        CredentialsUnavailable,
        credentials_for,
        stored_verify_token,
        uses_platform_app,
    )

    await confirmar_password(principal, payload.password, proposito="meta_connection_credentials")
    connection = await _meta_connection_of(principal, connection_id)
    if connection is None:
        raise ApiError.not_found("Conexión no encontrada.")

    try:
        creds = await credentials_for(connection)
    except CredentialsUnavailable as exc:
        raise ApiError.bad_request(exc.message, exc.reason) from exc

    secrets: dict[str, str] = {}
    if creds.access_token:
        secrets["access_token"] = creds.access_token
    if not uses_platform_app(connection):
        if creds.app_secret:
            secrets["app_secret"] = creds.app_secret
        verify_token = await stored_verify_token(connection)
        if verify_token:
            secrets["verify_token"] = verify_token

    from core.api.routers.connections import audit_connection_action

    await audit_connection_action(
        principal, "meta_connection.credentials_revealed", str(connection.id)
    )
    response.headers["Cache-Control"] = "no-store"
    return MetaRevealedSecrets(secrets=secrets)


class TemplateCreate(BaseModel):
    """Alta de plantilla (texto; los encabezados con multimedia se suben aparte)."""

    name: str
    language: str = "es"
    category: str = "UTILITY"
    header_format: str = "NONE"
    header_text: str = ""
    header_param_name: str = ""
    header_param_example: str = ""
    body_text: str
    body_param_names: str = ""
    body_param_examples: str = ""
    footer_text: str = ""
    buttons_json: str = ""
    parameter_format: str = ""


@router.post("/connections/{connection_id}/templates", status_code=201)
async def create_connection_template(
    connection_id: str,
    payload: TemplateCreate,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Crea una plantilla en la WABA de la conexión (pendiente de revisión de Meta)."""
    import uuid as uuid_mod

    from core.admin.meta_numbers_service import resolve_waba_and_credentials
    from core.admin.views import _build_template_components
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_upload import TemplateUploadError
    from core.gateway.meta_templates import (
        TemplateRateLimitError,
        TemplateValidationError,
        create_template,
    )

    if payload.header_format in ("IMAGE", "VIDEO", "DOCUMENT"):
        raise ApiError.bad_request(
            "Los encabezados con multimedia aún no están soportados (pendiente: subida de archivo).",
            "media_header_not_supported",
        )
    if not payload.name.strip() or not payload.body_text.strip():
        raise ApiError.bad_request("Nombre y cuerpo son obligatorios.", "missing_fields")
    try:
        connection, waba, creds = await resolve_waba_and_credentials(
            uuid_mod.UUID(connection_id), principal.tenant_id
        )
    except ValueError as exc:
        raise ApiError.bad_request("Conexión inválida.", "invalid_connection") from exc
    except Exception as exc:
        # La falta de credenciales ya la absorbe el resolver (`creds is None` abajo): lo que
        # llega acá es un error interno (SQL, descifrado). Se registra y se responde un
        # mensaje fijo, sin el texto de la excepción.
        from core.shared.logger import get_logger

        get_logger(__name__).exception(
            "create_template_resolucion_fallida", connection_id=connection_id
        )
        raise ApiError.bad_request(
            "No hay conexión/WABA lista todavía.", "connection_not_ready"
        ) from exc
    if connection is None or waba is None or creds is None:
        raise ApiError.bad_request("No hay conexión/WABA lista todavía.", "connection_not_ready")

    form = {
        "name": payload.name.strip(),
        "language": payload.language.strip() or "es",
        "category": payload.category.strip() or "UTILITY",
        "header_format": payload.header_format,
        "header_text": payload.header_text,
        "header_param_name": payload.header_param_name,
        "header_param_example": payload.header_param_example,
        "body_text": payload.body_text,
        "body_param_names": payload.body_param_names,
        "body_param_examples": payload.body_param_examples,
        "footer_text": payload.footer_text,
        "buttons_json": payload.buttons_json,
        "parameter_format": payload.parameter_format,
    }
    components = _build_template_components(form)
    try:
        await create_template(
            waba.waba_id,
            creds.access_token,
            creds.app_secret,
            creds.graph_api_version,
            connection.id,
            principal.tenant_id,
            waba.id,
            name=form["name"],
            language=form["language"],
            category=form["category"],
            components=components,
            parameter_format=form.get("parameter_format") or None,
        )
    except (TemplateValidationError, TemplateRateLimitError, TemplateUploadError) as exc:
        raise ApiError.bad_request(str(exc)[:200], "template_rejected") from exc
    except GraphError as exc:
        raise ApiError.bad_request(str(getattr(exc, "message", exc))[:200], "graph_error") from exc
    return Response(status_code=201)


# ------------------------------------------------- token BYOK (extender/inspeccionar)
class TokenConnectionRequest(BaseModel):
    connection_id: str


class TokenInspect(BaseModel):
    is_valid: bool
    type: str | None = None
    app_id: str | None = None
    # `expires_at` en unixtime (0 = no expira) según Meta; `token_expires_at`
    # es lo que ya teníamos guardado en el token store.
    expires_at: int | None = None
    never_expires: bool = False
    token_expires_at: datetime | None = None
    scopes: list[str] = []
    has_whatsapp_scopes: bool = False


class TokenExtend(BaseModel):
    token_expires_at: datetime | None = None
    expires_in: int | None = None


async def _resolve_connection_credentials(
    principal: Principal, connection_id: str, *, require_byok: bool = False
) -> tuple[Any, MetaCredentials]:
    """(conexión, credenciales) de ESA conexión según su origen, o error claro.

    El token y el secret viven bajo RLS de tenant (custodia del token store):
    todo se resuelve dentro de `tenant_scope` (mismo bug real que el webhook).
    Antes el secreto salía de "el" secreto efectivo del tenant, que con varias
    conexiones podía no ser el de esta (HU-CONN-01). Con `require_byok`, una conexión
    de otro origen responde 400 `not_byok` antes de tocar el token store.
    """
    from core.db.models import MetaConnection
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
    from core.shared.tenant import tenant_scope

    try:
        conn_uuid = uuid.UUID(connection_id)
    except ValueError as exc:
        raise ApiError.not_found("La conexión no existe.") from exc

    async with tenant_scope(principal.tenant_id):
        async with get_session() as session:
            connection = await session.get(MetaConnection, conn_uuid)
        if (
            connection is None
            or connection.tenant_id != principal.tenant_id
            or connection.deleted_at is not None
        ):
            raise ApiError.not_found("La conexión no existe.")
        if require_byok and connection.origin != "byok":
            raise ApiError.bad_request(
                "Solo el token de una conexión BYOK se puede extender desde aquí.", "not_byok"
            )
        try:
            creds = await credentials_for(connection)
        except CredentialsUnavailable as exc:
            raise ApiError.bad_request(
                "La conexión no tiene app secret o access token guardados.",
                "missing_credentials",
            ) from exc
    return connection, creds


@router.post("/byok/token/inspect", response_model=TokenInspect)
async def inspect_byok_token(
    payload: TokenConnectionRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TokenInspect:
    """Debug del token guardado: validez, tipo, vencimiento real y scopes."""
    from core.gateway.meta_credentials import access_token_expires_at
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_token_tools import inspect_token

    connection, creds = await _resolve_connection_credentials(principal, payload.connection_id)
    try:
        info = await inspect_token(
            creds.app_id, creds.app_secret, creds.access_token, version=creds.graph_api_version
        )
    except GraphError as exc:
        raise ApiError.bad_request(str(getattr(exc, "message", exc))[:200], "graph_error") from exc
    from core.shared.tenant import tenant_scope

    async with tenant_scope(principal.tenant_id):
        # El vencimiento guardado del MISMO token que se acaba de inspeccionar.
        guardado = await access_token_expires_at(connection)
    return TokenInspect(
        is_valid=info["is_valid"],
        type=info.get("type"),
        app_id=info.get("app_id"),
        expires_at=info.get("expires_at"),
        never_expires=bool(info.get("never_expires")),
        token_expires_at=guardado,
        scopes=list(info.get("scopes") or []),
        has_whatsapp_scopes=bool(info.get("has_whatsapp_scopes")),
    )


@router.post("/byok/token/extend", response_model=TokenExtend)
async def extend_byok_token(
    payload: TokenConnectionRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TokenExtend:
    """Canjea el token guardado por uno long-lived (~60 días) y lo persiste.

    Nunca devuelve el token: el panel solo muestra la nueva caducidad.
    """
    _require_owner_or_master(principal)

    from datetime import UTC, timedelta

    from core.gateway import meta_token_store
    from core.gateway.meta_byok import CLOUD_API_ACCESS_TOKEN_KIND
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_token_tools import extend_token
    from core.shared.tenant import tenant_scope

    # El token nuevo se guarda como el de una conexión BYOK; el de las demás es un token
    # de system user de la plataforma que no se canjea desde acá. Se valida el origen
    # antes de resolver credenciales: una conexión ES responde `not_byok`, no
    # `missing_credentials`.
    connection, creds = await _resolve_connection_credentials(
        principal, payload.connection_id, require_byok=True
    )
    try:
        info = await extend_token(
            creds.app_id, creds.app_secret, creds.access_token, version=creds.graph_api_version
        )
    except GraphError as exc:
        raise ApiError.bad_request(str(getattr(exc, "message", exc))[:200], "graph_error") from exc

    expires_in = info.get("expires_in")
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
    async with tenant_scope(principal.tenant_id):
        await meta_token_store.put_token(
            connection.id,
            CLOUD_API_ACCESS_TOKEN_KIND,
            connection.meta_app_id,
            info["access_token"],
            tenant_id=principal.tenant_id,
            expires_at=expires_at,
        )
    return TokenExtend(token_expires_at=expires_at, expires_in=expires_in)


# ------------------------------------------------- checklist de puesta en marcha
class ChecklistStep(BaseModel):
    key: str
    label: str
    link: str
    done: bool = False
    notes: str = ""


class PreflightOut(BaseModel):
    key: str
    # `None` = no evaluable en este entorno (los checks de producción devuelven
    # False/None fuera de un despliegue real; se muestran tal cual).
    ok: bool | None = None
    detail: str | None = None


class ChecklistOut(BaseModel):
    steps: list[ChecklistStep]
    checks: list[PreflightOut] = []
    meta_configured: bool = False


class ChecklistStepIn(BaseModel):
    step_key: str
    done: bool = False
    notes: str = ""


async def _checklist_steps(tenant_id: Any) -> tuple[list[ChecklistStep], bool]:
    """Pasos manuales de Meta con su estado (checklist del tenant)."""

    from core.db.models import MetaSetupChecklist
    from core.gateway.meta_preflight import MANUAL_STEPS, resolve_app_credentials

    config = await resolve_app_credentials(tenant_id)
    base = (
        f"https://developers.facebook.com/apps/{config.app_id}/"
        if config
        else "https://developers.facebook.com/apps/"
    )
    async with get_session() as session:
        filas = (
            (
                await session.execute(
                    select(MetaSetupChecklist).where(MetaSetupChecklist.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    estado = {f.step_key: f for f in filas}
    return (
        [
            ChecklistStep(
                key=paso["key"],
                label=paso["label"],
                link=base + paso.get("link_suffix", ""),
                done=bool(getattr(estado.get(paso["key"]), "done", False)),
                notes=getattr(estado.get(paso["key"]), "notes", "") or "",
            )
            for paso in MANUAL_STEPS
        ],
        config is not None,
    )


@router.get("/checklist/requirements", response_model=ChecklistOut)
async def checklist_requirements(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ChecklistOut:
    """Requisitos de puesta en marcha + los checks automáticos de Meta."""
    from core.db.models import Tenant
    from core.gateway.meta_preflight import run_preflight

    steps, meta_configured = await _checklist_steps(principal.tenant_id)
    slug = ""
    async with get_session() as session:
        tenant = await session.get(Tenant, principal.tenant_id)
        slug = tenant.slug if tenant else ""
    checks = await run_preflight(
        principal.tenant_id, slug, include_platform_app=actua_como_master(principal.admin)
    )
    return ChecklistOut(
        steps=steps,
        checks=[PreflightOut(key=c.key, ok=c.ok, detail=c.detail) for c in checks],
        meta_configured=meta_configured,
    )


@router.get("/checklist/production", response_model=ChecklistOut, deprecated=True)
async def checklist_production(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ChecklistOut:
    """Obsoleta (HU-CONN-07): los pasos manuales de Meta viven en `/checklist/requirements`
    y el checklist de producción es por conexión
    (`/connections/{connection_id}/production`). Se mantiene para la app móvil."""
    steps, meta_configured = await _checklist_steps(principal.tenant_id)
    return ChecklistOut(steps=steps, meta_configured=meta_configured)


@router.post("/checklist/step", status_code=204)
async def checklist_step(
    payload: ChecklistStepIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Marca o desmarca un paso del checklist (con nota opcional)."""

    from core.db.models import MetaSetupChecklist

    if not payload.step_key.strip():
        raise ApiError.bad_request("Falta el paso.", "missing_step")
    async with get_session() as session:
        fila = (
            await session.execute(
                select(MetaSetupChecklist).where(
                    MetaSetupChecklist.tenant_id == principal.tenant_id,
                    MetaSetupChecklist.step_key == payload.step_key,
                )
            )
        ).scalar_one_or_none()
        if fila is None:
            fila = MetaSetupChecklist(tenant_id=principal.tenant_id, step_key=payload.step_key)
            session.add(fila)
        fila.done = payload.done
        fila.notes = payload.notes.strip() or None
    return Response(status_code=204)


@router.post("/checklist/production/run", response_model=list[PreflightOut], deprecated=True)
async def run_production_checks(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> list[PreflightOut]:
    """Obsoleta (HU-CONN-07): usar `/connections/{connection_id}/production/run`.

    Ya no corre nada en el request ni muestra los checks de la plataforma (son del master):
    encola el checklist de producción de cada conexión del negocio y devuelve el último
    resultado guardado de cada una. Se mantiene para la app móvil."""
    from core.admin import production_readiness_service as readiness

    tenant_id = _tenant_or_403(principal)
    out: list[PreflightOut] = []
    for connection_id in await readiness.live_connection_ids(tenant_id):
        try:
            await readiness.request_connection_run(tenant_id, connection_id, principal.admin.id)
        except (readiness.ConnectionNotFound, readiness.EnqueueFailed):
            pass
        latest = await readiness.latest_connection_run(tenant_id, connection_id)
        for item in latest.checks if latest is not None and latest.status == "done" else []:
            out.append(
                PreflightOut(
                    key=f"{str(connection_id)[:8]}:{item['key']}",
                    ok=item.get("ok"),
                    detail=item.get("detail"),
                )
            )
    if not out:
        out.append(
            PreflightOut(
                key="production_checks",
                ok=None,
                detail="La revisión de cada conexión quedó en curso: consulta de nuevo en unos segundos.",
            )
        )
    return out


# ------------------------------------------------ checklist de producción por conexión
class ConnectionProduction(ReadinessRun):
    connection_id: uuid.UUID
    channel: str
    environment: str
    origin: str


def _tenant_or_403(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.forbidden("Selecciona un negocio.")
    return principal.tenant_id


def _connection_uuid(connection_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(connection_id)
    except ValueError as exc:
        raise ApiError.not_found("Conexión no encontrada.") from exc


async def _connection_production(
    tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> ConnectionProduction:
    from core.admin.production_readiness_service import latest_connection_run, owned_connection

    connection = await owned_connection(tenant_id, connection_id)
    if connection is None:
        raise ApiError.not_found("Conexión no encontrada.")
    run = await latest_connection_run(tenant_id, connection_id)
    return ConnectionProduction(
        connection_id=connection.id,
        channel=connection.channel,
        environment=connection.environment,
        origin=connection.origin,
        **readiness_run_out(run),
    )


@router.get("/connections/{connection_id}/production", response_model=ConnectionProduction)
async def get_connection_production(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ConnectionProduction:
    """Checklist de producción de la conexión: el último resultado guardado, con fecha.

    Número registrado, WABA suscrita, webhook, plantillas mínimas aprobadas y token, evaluados
    con las credenciales de ESTA conexión. No corre nada: para revisar, `POST .../run`."""
    tenant_id = _tenant_or_403(principal)
    return await _connection_production(tenant_id, _connection_uuid(connection_id))


@router.post(
    "/connections/{connection_id}/production/run",
    response_model=ConnectionProduction,
    status_code=202,
)
async def run_connection_production(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConnectionProduction:
    """Encola la revisión de producción de la conexión (job de Celery) y responde al instante
    con la corrida `queued` (o la que ya estaba en curso). El panel consulta el GET hasta que
    termine."""
    from core.admin import production_readiness_service as readiness

    tenant_id = _tenant_or_403(principal)
    cid = _connection_uuid(connection_id)
    try:
        await readiness.request_connection_run(tenant_id, cid, principal.admin.id)
    except readiness.ConnectionNotFound as exc:
        raise ApiError.not_found("Conexión no encontrada.") from exc
    except readiness.EnqueueFailed as exc:
        raise ApiError(503, "enqueue_failed", str(exc)) from exc
    return await _connection_production(tenant_id, cid)

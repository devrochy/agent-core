"""Wizard de Embedded Signup de WhatsApp: máquina de estados persistida y
reanudable (`ConnectionWizard`). Único módulo que debe escribir esa tabla
-- verificado por `tests/test_meta_onboarding_no_persiste_secretos.py`.

Es la app del ISV/framework (`settings.meta_app_id`/`meta_app_secret`/
`meta_config_id`) la que hace el Embedded Signup en nombre de cada tenant --
no es BYOK como `meta_preflight.resolve_app_credentials` (eso es para
tenants que YA tienen su propia app y conexión). Por eso acá se usan esas
3 settings directo, no `resolve_app_credentials`.

REGLA DURA: ninguna función de este módulo acepta ni persiste un `code` de
Embedded Signup ni un token de acceso en `ConnectionWizard.context`/
`last_error` -- los secretos van a `meta_token_store` (AAD tenant+conexión+
tipo), nunca a esta tabla (RLS del propio tenant, no custodia de secretos).
Ver ADR-0030.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select

from core.config import get_settings
from core.db.models import (
    WIZARD_STEPS,
    ConnectionWizard,
    MessageTemplate,
    MetaConnection,
    PhoneNumber,
    WabaAccount,
    utcnow,
)
from core.db.session import get_owned, get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import graph_fetch
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
_TIMEOUT_S = 15.0
TOKEN_KIND = "business_integration_system_user"

# object del popup -> canal (mismo mapeo que webhook_flatten.py/meta_subscriptions.py)
_EVENT_COEXISTENCE = "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
_EVENT_ONLY_WABA = "FINISH_ONLY_WABA"
_EVENT_FINISH = "FINISH"


class WizardError(AgentCoreError):
    """Error de esta capa (wizard no encontrado, code expirado, paso
    inválido) -- distinto de `GraphError`, que es un error de Graph API."""


@dataclass
class ConnectionWizardView:
    """Vista seguro-de-exponer de un `ConnectionWizard` (sin nada sensible,
    apto para pasar a la plantilla o devolver en JSON)."""

    id: uuid.UUID
    connection_id: uuid.UUID | None
    channel: str
    environment: str
    current_step: str
    completed_steps: list[str]
    last_error: dict[str, Any] | None


def _to_view(wizard: ConnectionWizard) -> ConnectionWizardView:
    return ConnectionWizardView(
        id=wizard.id,
        connection_id=wizard.connection_id,
        channel=wizard.channel,
        environment=wizard.environment,
        current_step=wizard.current_step,
        completed_steps=list(wizard.completed_steps or []),
        last_error=wizard.last_error,
    )


def _mark_done(wizard: ConnectionWizard, step: str) -> None:
    if step not in wizard.completed_steps:
        wizard.completed_steps = [*wizard.completed_steps, step]


def _advance_to(wizard: ConnectionWizard, step: str) -> None:
    _mark_done(wizard, wizard.current_step)
    wizard.current_step = step


async def _get_wizard(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> ConnectionWizard:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant")
        return wizard


async def start_or_resume_wizard(
    tenant_id: uuid.UUID, channel: str = "whatsapp", environment: str = "production", *, admin_id: uuid.UUID | None = None
) -> ConnectionWizardView:
    """Reanuda el wizard "vivo" (`current_step != 'done'`) de este tenant+
    canal+entorno si existe y no expiró; si expiró, lo cierra y arranca uno
    nuevo. Si ya hay una `MetaConnection` `connected` para ese tenant+canal,
    entra en modo REAUTORIZACIÓN: reusa esa conexión en vez de crear una
    nueva, y arranca directo en `launch_oauth` (se salta `select_channel`)."""
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_app_secret or not settings.meta_config_id:
        raise WizardError(
            "META_APP_ID/META_APP_SECRET/META_CONFIG_ID no están configurados -- "
            "el wizard de Embedded Signup necesita la app del framework, no BYOK."
        )

    async with get_session() as session:
        existing = (
            await session.execute(
                select(ConnectionWizard).where(
                    ConnectionWizard.tenant_id == tenant_id,
                    ConnectionWizard.channel == channel,
                    ConnectionWizard.environment == environment,
                    ConnectionWizard.current_step != "done",
                )
            )
        ).scalar_one_or_none()

        if existing is not None and existing.expires_at > utcnow():
            return _to_view(existing)

        if existing is not None:
            # Expiró: se cierra (no se borra -- queda como historial de abandono)
            # para liberar el índice único parcial y poder crear una fila nueva.
            existing.current_step = "done"
            existing.last_error = {"code": "wizard_expired", "message": "El wizard expiró (>24h) y se reinició"}
            await session.flush()

        connected = (
            await session.execute(
                select(MetaConnection)
                .where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == channel,
                    MetaConnection.environment == environment,
                    MetaConnection.status == "connected",
                )
                .order_by(MetaConnection.created_at.desc())
            )
        ).scalars().first()

        if connected is not None:
            connection_id = connected.id
            current_step = "launch_oauth"
        else:
            # Reutiliza cualquier MetaConnection viva (draft/pending/degraded)
            # del mismo tenant+canal+entorno: el índice único parcial
            # `uq_meta_connections_tenant_app` no admite una segunda fila no
            # borrada para la misma app. Sin esto, reiniciar un wizard vencido
            # revienta con IntegrityError (HU-CONN-02).
            reutilizable = (
                await session.execute(
                    select(MetaConnection)
                    .where(
                        MetaConnection.tenant_id == tenant_id,
                        MetaConnection.channel == channel,
                        MetaConnection.environment == environment,
                        MetaConnection.deleted_at.is_(None),
                    )
                    .order_by(MetaConnection.created_at.desc())
                )
            ).scalars().first()
            if reutilizable is not None:
                connection_id = reutilizable.id
            else:
                connection = MetaConnection(
                    tenant_id=tenant_id,
                    channel=channel,
                    environment=environment,
                    origin="embedded_signup",
                    meta_app_id=settings.meta_app_id,
                    meta_config_id=settings.meta_config_id,
                    graph_api_version=settings.graph_api_version,
                    status="draft",
                )
                session.add(connection)
                await session.flush()
                connection_id = connection.id
            current_step = "select_channel"

        wizard = ConnectionWizard(
            tenant_id=tenant_id,
            connection_id=connection_id,
            channel=channel,
            environment=environment,
            current_step=current_step,
            started_by=admin_id,
        )
        session.add(wizard)
        await session.flush()
        return _to_view(wizard)


async def record_cancel_or_progress(
    wizard_id: uuid.UUID,
    *,
    current_step: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Escrito por el listener de `postMessage` en el evento `CANCEL` --
    analítica de abandono. NUNCA acepta `code` ni token (no hay parámetro
    para eso, a propósito): esta función no puede persistir un secreto
    aunque quisiera."""
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id)
        if wizard is None:
            return
        if error_code or error_message or error_id:
            wizard.last_error = {"code": error_code, "message": error_message, "error_id": error_id}
        abandono = dict(wizard.context or {})
        abandono["abandono"] = {"current_step": current_step, "session_id": session_id, "at": utcnow().isoformat()}
        wizard.context = abandono


async def persist_finish_payload(
    wizard_id: uuid.UUID,
    event_type: str,
    *,
    waba_id: str | None = None,
    phone_number_id: str | None = None,
    business_id: str | None = None,
) -> ConnectionWizardView:
    """Persiste lo que el popup YA dio, sin esperar al canje del `code` --
    si el canje falla o el navegador se cierra, estos IDs no se pierden."""
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant")

        connection = await session.get(MetaConnection, wizard.connection_id) if wizard.connection_id else None
        if connection is not None and business_id:
            connection.business_id = business_id
        if connection is not None and event_type == _EVENT_COEXISTENCE:
            connection.health = {**(connection.health or {}), "coexistence": True}

        if waba_id:
            existing_waba = (
                await session.execute(
                    select(WabaAccount).where(WabaAccount.waba_id == waba_id, WabaAccount.connection_id == wizard.connection_id)
                )
            ).scalar_one_or_none()
            if existing_waba is None:
                session.add(WabaAccount(tenant_id=tenant_id, connection_id=wizard.connection_id, waba_id=waba_id))

        if phone_number_id:
            # tenant_id explícito, no solo la unicidad global de
            # phone_number_id (defensa en profundidad -- hallazgo
            # auditor-multitenant, 2026-09-22).
            existing_phone = (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.phone_number_id == phone_number_id, PhoneNumber.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if existing_phone is None:
                session.add(
                    PhoneNumber(
                        tenant_id=tenant_id, connection_id=wizard.connection_id, phone_number_id=phone_number_id
                    )
                )

        if event_type == _EVENT_COEXISTENCE:
            _mark_done(wizard, "register_number")

        await session.flush()
        return _to_view(wizard)


async def exchange_code(wizard_id: uuid.UUID, code: str) -> ConnectionWizardView:
    """Time-critical: el `code` de Meta tiene TTL de 30s. Sin `redirect_uri`
    primero (no confirmado contra doc primaria de Meta al momento de
    escribir esto -- la doc devolvió 429). Si Meta responde OAuthException
    mencionando `redirect_uri`, se reintenta UNA vez agregándolo."""
    tenant_id = get_current_tenant_id()
    settings = get_settings()
    started_at = time.monotonic()

    wizard = await _get_wizard(wizard_id, tenant_id)
    if wizard.connection_id is None:
        raise WizardError("El wizard no tiene una conexión asociada todavía")

    version = settings.graph_api_version
    params = {"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": code}
    call_started_at = time.monotonic()

    token, used_redirect_uri = await _fetch_exchange_token(version, params, settings)

    latency_ms = int((time.monotonic() - started_at) * 1000)
    dispatch_ms = int((call_started_at - started_at) * 1000)
    log.info(
        "embedded_signup_exchange_latency",
        wizard_id=str(wizard_id),
        latency_ms=latency_ms,
        dispatch_ms=dispatch_ms,
        used_redirect_uri=used_redirect_uri,
    )

    # Guarda el token ANTES de cualquier otra llamada a Meta -- si el
    # navegador se cierra justo después, la conexión es reanudable sin
    # repetir el OAuth (criterio de aceptación explícito del ticket).
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await session.get(MetaConnection, wizard.connection_id)
        business_id = connection.business_id if connection else None
        subject_id = business_id or str(wizard.connection_id)

    await meta_token_store.put_token(wizard.connection_id, TOKEN_KIND, subject_id, token, tenant_id=tenant_id)

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await session.get(MetaConnection, wizard.connection_id)
        if connection is not None:
            connection.status = "pending"
            # El wizard puede reutilizar una conexión viva de otro origen; desde que
            # guarda el token del system user la conexión es de Embedded Signup.
            connection.origin = "embedded_signup"
        _advance_to(wizard, "discover_assets")
        await session.flush()
        return _to_view(wizard)


async def _fetch_exchange_token(version: str, params: dict[str, str], settings: Any) -> tuple[str, bool]:
    async def _call(extra_params: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            return await client.get(f"{GRAPH_BASE}/{version}/oauth/access_token", params={**params, **extra_params})

    try:
        response = await _call({})
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise GraphError(f"No se pudo contactar a Meta para canjear el code: {exc}", category="TRANSIENT") from exc

    if response.status_code >= 400:
        body = _safe_json(response)
        log.warning("embedded_signup_exchange_error_raw", body_redacted=_redact(body))
        error = classify_and_build_error(http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text)
        message = (error.error_user_msg or error.message or "").lower()
        if "redirect_uri" in message:
            # Hallazgo documentado (no confirmado contra doc primaria, ver
            # docstring del módulo): en al menos algún caso real Meta exigió
            # redirect_uri incluso en el flujo de popup. Se reintenta UNA vez.
            redirect_uri = f"{settings.public_base_url.rstrip('/')}/admin/conexiones/wizard/whatsapp"
            try:
                response2 = await _call({"redirect_uri": redirect_uri})
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                raise GraphError(f"No se pudo contactar a Meta (reintento con redirect_uri): {exc}", category="TRANSIENT") from exc
            if response2.status_code >= 400:
                body2 = _safe_json(response2)
                raise classify_and_build_error(
                    http_status=response2.status_code, body=body2, headers=response2.headers, raw_text=response2.text
                )
            token = _extract_token(response2)
            return token, True
        raise error

    token = _extract_token(response)
    return token, False


def _extract_token(response: httpx.Response) -> str:
    data = _safe_json(response) or {}
    token = data.get("access_token")
    if not token:
        raise WizardError("Meta no devolvió access_token en el canje")
    return str(token)


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        return response.json()
    except Exception:
        return None


def _redact(body: dict[str, Any] | None) -> dict[str, Any] | None:
    if body is None:
        return None
    return {k: ("<redactado>" if "token" in k.lower() else v) for k, v in body.items()}


async def run_automatic_steps(wizard_id: uuid.UUID) -> ConnectionWizardView:
    """Ejecuta en cadena los pasos que NO requieren input del admin, hasta
    llegar a uno que sí lo requiere (`register_number` sin PIN todavía) o a
    `done`. Cada paso es idempotente: si ya está en `completed_steps`, no
    vuelve a llamar a Meta."""
    tenant_id = get_current_tenant_id()
    wizard = await _get_wizard(wizard_id, tenant_id)

    step_order = list(WIZARD_STEPS)
    while wizard.current_step not in ("register_number", "done") and wizard.current_step in step_order:
        step = wizard.current_step
        if step == "discover_assets":
            wizard = await _discover_assets_step(wizard_id, tenant_id)
        elif step == "select_assets":
            wizard = await _select_assets_step(wizard_id, tenant_id)
        elif step == "subscribe_webhook":
            wizard = await _subscribe_webhook_step(wizard_id, tenant_id)
        elif step == "sync_templates":
            wizard = await _sync_templates_step(wizard_id, tenant_id)
        elif step == "verify_health":
            wizard = await _verify_health_step(wizard_id, tenant_id)
        else:
            break  # select_channel/launch_oauth/exchange_code: esperan al frontend

    if wizard.current_step == "register_number" and "register_number" in (wizard.completed_steps or []):
        wizard = await _advance_past_register(wizard_id, tenant_id)
        return await run_automatic_steps(wizard.id)

    return _to_view(wizard)


async def _load_connection(wizard: ConnectionWizard, session) -> MetaConnection | None:
    if wizard.connection_id is None:
        return None
    return await session.get(MetaConnection, wizard.connection_id)


async def _discover_assets_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await _load_connection(wizard, session)
        waba = (
            await session.execute(select(WabaAccount).where(WabaAccount.connection_id == wizard.connection_id))
        ).scalars().first()
        phone = (
            await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == wizard.connection_id))
        ).scalars().first()
        settings = get_settings()
        version = connection.graph_api_version if connection else settings.graph_api_version

    token = await meta_token_store.get_token(
        wizard.connection_id, TOKEN_KIND, (connection.business_id if connection else None) or str(wizard.connection_id),
        tenant_id=tenant_id,
    )
    if token and waba is not None and not waba.raw:
        data = await graph_fetch(
            f"/{waba.waba_id}", token=token, app_secret=settings.meta_app_secret,
            query={"fields": "name,currency,timezone_id,message_template_namespace,account_review_status"},
            version=version, connection_id=wizard.connection_id,
        )
        async with get_session() as session:
            waba_row = await session.get(WabaAccount, waba.id)
            waba_row.name = data.get("name")
            waba_row.currency = data.get("currency")
            waba_row.timezone_id = data.get("timezone_id")
            waba_row.message_template_namespace = data.get("message_template_namespace")
            waba_row.account_review_status = data.get("account_review_status")
            waba_row.raw = data

    if token and phone is not None and not phone.raw:
        data = await graph_fetch(
            f"/{phone.phone_number_id}", token=token, app_secret=settings.meta_app_secret,
            query={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier,name_status,code_verification_status,platform_type,is_on_biz_app"},
            version=version, connection_id=wizard.connection_id,
        )
        async with get_session() as session:
            phone_row = await session.get(PhoneNumber, phone.id)
            phone_row.display_phone_number = data.get("display_phone_number")
            phone_row.verified_name = data.get("verified_name")
            phone_row.quality_rating = data.get("quality_rating")
            phone_row.messaging_limit_tier = data.get("messaging_limit_tier")
            phone_row.name_status = data.get("name_status")
            phone_row.code_verification_status = data.get("code_verification_status")
            phone_row.platform_type = data.get("platform_type")
            phone_row.raw = data

    if token:
        await _verify_token_scopes(token, version, settings, wizard.connection_id, waba.waba_id if waba else None)

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "select_assets")
        await session.flush()
        return _to_view(wizard)


# Nombres de scope vistos en la práctica para el mismo permiso -- evidencia
# de 2026 de que Meta devuelve `granular_scopes` con nombres distintos
# según la app/momento (`whatsapp_business_manage_events` en vez de
# `whatsapp_business_management`), mientras el array plano `scopes` sí trae
# el nombre esperado. Se aceptan todos, nunca se aborta si no matchea.
_WHATSAPP_MANAGEMENT_SCOPE_NAMES = {"whatsapp_business_management", "whatsapp_business_manage_events"}
_debug_token_logged_once = False


async def _verify_token_scopes(
    token: str, version: str, settings: Any, connection_id: uuid.UUID, waba_id: str | None
) -> None:
    """VERIFICACIÓN/FALLBACK, no gate: `waba_id` del popup (ya persistido
    por `persist_finish_payload`) es la fuente PRIMARIA -- esto solo es para
    detectar de antemano un token con permisos insuficientes y dejarlo
    registrado en `connection.health` para soporte. La forma exacta del
    JSON de `/debug_token` no está verificada contra doc primaria: parseo
    100% tolerante (`.get()` con default en cada campo), nunca levanta
    excepción."""
    global _debug_token_logged_once
    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
    try:
        data = await graph_fetch(
            "/debug_token", token=app_token, app_secret=settings.meta_app_secret,
            query={"input_token": token}, version=version, connection_id=connection_id,
        )
    except GraphError as exc:
        log.warning("embedded_signup_debug_token_fallo", detail=exc.message)
        return

    if not _debug_token_logged_once:
        log.info("embedded_signup_debug_token_raw_shape", body_redacted=_redact(data or {}))
        _debug_token_logged_once = True

    payload = (data or {}).get("data", {}) or {}
    flat_scopes = set(payload.get("scopes") or [])
    granular = payload.get("granular_scopes") or []
    granular_scope_names = {g.get("scope") for g in granular if isinstance(g, dict)}
    has_whatsapp_scope = bool((flat_scopes | granular_scope_names) & _WHATSAPP_MANAGEMENT_SCOPE_NAMES)

    target_ids: list[str] = []
    for g in granular:
        if isinstance(g, dict) and g.get("scope") in _WHATSAPP_MANAGEMENT_SCOPE_NAMES:
            target_ids.extend(g.get("target_ids") or [])

    if not has_whatsapp_scope:
        log.warning("embedded_signup_token_sin_scope_whatsapp", connection_id=str(connection_id))
    if waba_id and target_ids and waba_id not in target_ids:
        log.warning(
            "embedded_signup_token_target_ids_no_incluye_waba",
            connection_id=str(connection_id), waba_id=waba_id, target_ids=target_ids,
        )
    elif not target_ids:
        log.warning("embedded_signup_token_sin_target_ids", connection_id=str(connection_id))

    async with get_session() as session:
        connection = await session.get(MetaConnection, connection_id)
        if connection is not None:
            connection.health = {
                **(connection.health or {}),
                "token_scope_check": {
                    "has_whatsapp_scope": has_whatsapp_scope,
                    "target_ids_present": bool(target_ids),
                },
            }


async def _select_assets_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    """No-op idempotente: Meta ya hizo la selección dentro del popup. Este
    paso solo existe para el caso `FINISH_ONLY_WABA` (fase 6, sin número
    todavía) -- el admin carga el número más tarde y ese flujo re-dispara
    `discover_assets` para ese número."""
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "subscribe_webhook")
        await session.flush()
        return _to_view(wizard)


async def _record_graph_error(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None, exc: GraphError, *, at_step: str) -> None:
    """Persiste code/error_subcode/fbtrace_id en `connection_wizards.
    last_error` -- criterio de aceptación explícito del ticket, para que
    soporte pueda diagnosticar sin pedirle logs al admin."""
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            return
        wizard.last_error = {
            "code": exc.code, "error_subcode": exc.error_subcode, "fbtrace_id": exc.fbtrace_id,
            "message": exc.message, "at_step": at_step,
        }


def _humanize_subscribed_apps_error(exc: GraphError) -> str:
    if exc.http_status == 403:
        return (
            "El token no tiene whatsapp_business_management sobre esta WABA, o no se completó "
            "el Embedded Signup -- hay que volver a conectar."
        )
    if exc.http_status == 404:
        return "WABA no encontrada."
    if exc.http_status == 422:
        from core.gateway.meta_subscriptions import humanize_subscription_error

        return humanize_subscription_error(exc)
    return f"Error de Meta: {exc.message}"


async def _subscribe_waba_to_app(
    token: str, waba_id: str, version: str, connection_id: uuid.UUID, app_secret: str, tenant_id: uuid.UUID | None = None,
    *, override_callback_uri: str | None = None, override_verify_token: str | None = None,
) -> None:
    """`POST /{waba_id}/subscribed_apps`: la suscripción de OBJETO -- distinta
    de `meta_subscriptions.upsert_app_subscription` (suscripción de APP,
    `POST /{app_id}/subscriptions`, PR #43). Sin esta, la WABA nunca manda
    eventos aunque la app esté bien configurada.

    REGLA DE DISEÑO INNEGOCIABLE (ticket): aunque se use `override_callback_
    uri` acá, el endpoint global de la app (`/webhooks/whatsapp`) NO se
    puede retirar -- los webhooks de plantillas (`message_template_*`) y de
    cuenta (`account_update`/`account_alerts`/`account_review_update`) están
    configurados a nivel de APP (`meta_subscriptions.OBJECT_FIELDS`) y
    SIEMPRE van al callback por defecto, nunca a un override -- un override
    de `subscribed_apps` es por-WABA y solo afecta el tráfico de MENSAJES de
    esa WABA puntual.

    Verificación OBLIGATORIA inmediata (GET) antes de dar el paso por
    completo -- sin ella, un `{"success": true}` del POST no garantiza nada."""
    if override_callback_uri and len(override_callback_uri) > 200:
        raise WizardError("override_callback_uri supera los 200 caracteres permitidos por Meta")

    body: dict[str, str] | None = None
    if override_callback_uri:
        body = {"override_callback_uri": override_callback_uri}
        if override_verify_token:
            body["verify_token"] = override_verify_token

    await graph_fetch(
        f"/{waba_id}/subscribed_apps", method="POST", token=token, app_secret=app_secret,
        body=body, version=version, connection_id=connection_id,
    )

    verification = await graph_fetch(
        f"/{waba_id}/subscribed_apps", token=token, app_secret=app_secret, version=version, connection_id=connection_id,
    )
    subscribed = bool((verification or {}).get("data"))

    # tenant_id explícito -- defensa en profundidad, mismo criterio ya
    # aplicado en persist_finish_payload/sync_phone_numbers (hallazgo
    # auditor-multitenant, 2026-09-22, severidad baja: connection_id acá
    # siempre viene de wizard.connection_id ya resuelto con tenant_id, pero
    # no cuesta nada repetir el filtro).
    async with get_session() as session:
        conditions = [WabaAccount.waba_id == waba_id, WabaAccount.connection_id == connection_id]
        if tenant_id is not None:
            conditions.append(WabaAccount.tenant_id == tenant_id)
        waba_row = (await session.execute(select(WabaAccount).where(*conditions))).scalar_one_or_none()
        if waba_row is not None:
            waba_row.subscribed = subscribed

    if not subscribed:
        raise WizardError(
            "Meta no confirmó la suscripción de esta WABA (GET /subscribed_apps devolvió vacío) "
            "-- sin esto no llega ningún webhook."
        )


async def _subscribe_webhook_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    from core.gateway.meta_preflight import AppCredentials
    from core.gateway.meta_subscriptions import SubscriptionError, upsert_app_subscription

    settings = get_settings()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await _load_connection(wizard, session)
        waba = (
            await session.execute(select(WabaAccount).where(WabaAccount.connection_id == wizard.connection_id))
        ).scalars().first()

    version = connection.graph_api_version if connection else settings.graph_api_version
    config = AppCredentials(
        app_id=settings.meta_app_id, app_secret=settings.meta_app_secret, config_id=settings.meta_config_id,
        graph_api_version=version, connection_id=wizard.connection_id,
    )
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    try:
        await upsert_app_subscription(
            config, "whatsapp_business_account", callback_url=callback_url, verify_token=settings.whatsapp_verify_token
        )
    except SubscriptionError as exc:
        log.warning("embedded_signup_subscribe_webhook_self_check_fallo", detail=str(exc))
    except GraphError as exc:
        log.warning("embedded_signup_subscribe_webhook_fallo", detail=exc.message)
        await _record_graph_error(wizard_id, tenant_id, exc, at_step="subscribe_webhook")

    if waba is not None:
        token = await meta_token_store.get_token(
            wizard.connection_id, TOKEN_KIND, (connection.business_id if connection else None) or str(wizard.connection_id),
            tenant_id=tenant_id,
        )
        if token and not waba.subscribed:
            try:
                await _subscribe_waba_to_app(token, waba.waba_id, version, wizard.connection_id, settings.meta_app_secret, tenant_id)
            except GraphError as exc:
                await _record_graph_error(wizard_id, tenant_id, exc, at_step="subscribe_webhook")
                raise WizardError(_humanize_subscribed_apps_error(exc)) from exc

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "register_number")
        await session.flush()
        return _to_view(wizard)


async def _advance_past_register(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "sync_templates")
        await session.flush()
        return _to_view(wizard)


PIN_KIND = "phone_register_pin"
PIN_MISMATCH_CODE = 133005
TOO_MANY_GUESSES_CODE = 133008


def _generate_pin() -> str:
    """PIN de 6 dígitos con `secrets` (no `random`): es un secreto real de
    two-step verification, no un identificador de UI."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def register_number_step(wizard_id: uuid.UUID) -> ConnectionWizardView:
    """Único paso con input humano en el sentido de "el admin hace clic",
    pero el PIN lo genera el sistema (nunca lo tipea nadie) -- se cifra en
    `meta_token_store` (kind dedicado, `PIN_KIND`) ANTES de llamar a Meta,
    mismo criterio que el token de la conexión. El PIN NUNCA aparece en
    logs ni en la respuesta de ninguna ruta del panel.

    Idempotente: si Meta ya reporta el número registrado, se trata como
    éxito. Si `coexistence=true`, el paso se saltea (chequeo explícito acá
    además del salto que ya hace `persist_finish_payload`, por si el modo
    reautorización llega a este paso sin haber vuelto a pasar por ahí)."""
    tenant_id = get_current_tenant_id()
    settings = get_settings()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant")
        connection = await _load_connection(wizard, session)
        phone = (
            await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == wizard.connection_id))
        ).scalars().first()

    if connection is not None and (connection.health or {}).get("coexistence"):
        async with get_session() as session:
            wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
            _advance_to(wizard, "sync_templates")
            await session.flush()
            return _to_view(wizard)

    if phone is None:
        raise WizardError("Todavía no hay un número asociado a esta conexión (falta cargarlo)")

    if (wizard.context or {}).get("register_number_locked"):
        raise WizardError(
            "Meta bloqueó temporalmente los intentos de registro de este número (demasiados intentos "
            "de PIN). Esperá un momento y pedí a soporte que lo reintente manualmente."
        )

    token = await meta_token_store.get_token(
        wizard.connection_id, TOKEN_KIND, (connection.business_id if connection else None) or str(wizard.connection_id),
        tenant_id=tenant_id,
    )
    if not token:
        raise WizardError("No hay token guardado todavía para esta conexión -- repetí el paso de conexión")

    pin = _generate_pin()
    # Se guarda ANTES de llamar a Meta -- reanudable si el navegador se
    # cierra justo después (mismo criterio que el token en exchange_code).
    await meta_token_store.put_token(wizard.connection_id, PIN_KIND, phone.phone_number_id, pin, tenant_id=tenant_id)

    try:
        await graph_fetch(
            f"/{phone.phone_number_id}/register", method="POST", token=token, app_secret=settings.meta_app_secret,
            body={"messaging_product": "whatsapp", "pin": pin},
            version=connection.graph_api_version if connection else settings.graph_api_version,
            connection_id=wizard.connection_id,
        )
    except GraphError as exc:
        await _record_graph_error(wizard_id, tenant_id, exc, at_step="register_number")
        if exc.code == PIN_MISMATCH_CODE:
            raise WizardError(
                "El número ya tenía verificación en dos pasos con OTRO PIN (Meta no tiene un endpoint "
                "para desactivarla). Opciones: (1) fijar un PIN nuevo para este número, o "
                "(2) pedirle al cliente que lo resetee desde WhatsApp Manager."
            ) from exc
        if exc.code == TOO_MANY_GUESSES_CODE:
            async with get_session() as session:
                wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
                wizard.context = {**(wizard.context or {}), "register_number_locked": True}
            raise WizardError(
                "Meta bloqueó temporalmente los intentos de PIN para este número (demasiados intentos). "
                "No se reintenta automáticamente -- esperá y pedí a soporte que lo reintente a mano."
            ) from exc
        if not ("already" in (exc.message or "").lower() and "regist" in (exc.message or "").lower()):
            raise  # cualquier otro error: no es idempotente, se propaga

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        phone_row = await session.get(PhoneNumber, phone.id)
        phone_row.registered = True
        phone_row.pin_set = True
        _advance_to(wizard, "sync_templates")
        await session.flush()
        return _to_view(wizard)


async def set_new_pin_step(wizard_id: uuid.UUID) -> ConnectionWizardView:
    """Remediación de un `133005` (PIN mismatch): Meta no tiene endpoint
    para desactivar two-step -- la salida es fijar un PIN nuevo directo
    con `POST /{phone_number_id}` (no `/register`). Limpia el lock de
    `133008` si estuviera activo (es una acción manual explícita del
    admin, no un reintento automático)."""
    tenant_id = get_current_tenant_id()
    settings = get_settings()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant")
        connection = await _load_connection(wizard, session)
        phone = (
            await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == wizard.connection_id))
        ).scalars().first()

    if phone is None:
        raise WizardError("Todavía no hay un número asociado a esta conexión")

    token = await meta_token_store.get_token(
        wizard.connection_id, TOKEN_KIND, (connection.business_id if connection else None) or str(wizard.connection_id),
        tenant_id=tenant_id,
    )
    if not token:
        raise WizardError("No hay token guardado todavía para esta conexión")

    new_pin = _generate_pin()
    await meta_token_store.put_token(wizard.connection_id, PIN_KIND, phone.phone_number_id, new_pin, tenant_id=tenant_id)

    await graph_fetch(
        f"/{phone.phone_number_id}", method="POST", token=token, app_secret=settings.meta_app_secret,
        body={"pin": new_pin}, version=connection.graph_api_version if connection else settings.graph_api_version,
        connection_id=wizard.connection_id,
    )

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        phone_row = await session.get(PhoneNumber, phone.id)
        phone_row.registered = True
        phone_row.pin_set = True
        wizard.context = {**(wizard.context or {}), "register_number_locked": False}
        _advance_to(wizard, "sync_templates")
        await session.flush()
        return _to_view(wizard)


async def _sync_templates_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    settings = get_settings()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await _load_connection(wizard, session)
        waba = (
            await session.execute(select(WabaAccount).where(WabaAccount.connection_id == wizard.connection_id))
        ).scalars().first()

    if waba is not None:
        token = await meta_token_store.get_token(
            wizard.connection_id, TOKEN_KIND, (connection.business_id if connection else None) or str(wizard.connection_id),
            tenant_id=tenant_id,
        )
        if token:
            try:
                data = await graph_fetch(
                    f"/{waba.waba_id}/message_templates", token=token, app_secret=settings.meta_app_secret,
                    query={"fields": "name,language,category,status,rejected_reason,quality_score,components"},
                    version=connection.graph_api_version if connection else settings.graph_api_version,
                    connection_id=wizard.connection_id,
                )
                for item in (data or {}).get("data", []):
                    await _upsert_template(waba.id, tenant_id, item)
            except GraphError as exc:
                log.warning("embedded_signup_sync_templates_fallo", detail=exc.message)

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "verify_health")
        await session.flush()
        return _to_view(wizard)


async def _upsert_template(waba_account_id: uuid.UUID, tenant_id: uuid.UUID | None, item: dict[str, Any]) -> None:
    async with get_session() as session:
        existing = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.waba_account_id == waba_account_id,
                    MessageTemplate.name == item.get("name", ""),
                    MessageTemplate.language == item.get("language", ""),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                MessageTemplate(
                    tenant_id=tenant_id,
                    waba_account_id=waba_account_id,
                    meta_template_id=item.get("id"),
                    name=item.get("name", ""),
                    language=item.get("language", ""),
                    category=item.get("category"),
                    status=item.get("status", "DRAFT"),
                    rejected_reason=item.get("rejected_reason"),
                    quality_score=str(item.get("quality_score")) if item.get("quality_score") else None,
                    components=item.get("components"),
                    synced_at=utcnow(),
                )
            )
        else:
            existing.status = item.get("status", existing.status)
            existing.rejected_reason = item.get("rejected_reason")
            existing.components = item.get("components")
            existing.synced_at = utcnow()


async def _verify_health_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    from core.gateway.meta_subscriptions import list_app_subscriptions

    settings = get_settings()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await _load_connection(wizard, session)

    subs = await list_app_subscriptions(settings.meta_app_id)
    sub_ok = any(s.object == "whatsapp_business_account" and s.active for s in subs)

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await session.get(MetaConnection, wizard.connection_id)
        if connection is not None:
            connection.status = "connected" if sub_ok else "degraded"
            connection.connected_at = utcnow()
            connection.last_health_check_at = utcnow()
            connection.health = {**(connection.health or {}), "webhook_subscription_active": sub_ok}
        _advance_to(wizard, "done")
        await session.flush()
        return _to_view(wizard)

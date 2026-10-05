"""Health checks de una `MetaConnection`: token, suscripción de WABA,
suscripción de app (delegada, no repetida), números, override de webhook,
plantillas, facturación (manual). Agrega los 7 resultados en el JSON que
se persiste en `MetaConnection.health` y decide el semáforo (`status`).

Ninguna función de acá loguea un token -- ver `core/shared/redact.py` y
el fingerprint ya guardado junto a cada secreto (`core/gateway/
meta_token_store.py`) para diagnóstico de errores sin exponer el valor real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from core.db.models import (
    MessageTemplate,
    MetaConnection,
    PhoneNumber,
    WabaAccount,
)
from core.db.session import get_session
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import GRAPH_BASE, graph_fetch
from core.gateway.meta_phone_numbers import _PHONE_FIELDS
from core.gateway.meta_preflight import PreflightCheck
from core.shared.logger import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 15.0

# object (Graph API, tabla de estado de suscripción de app) -> MetaConnection.channel
_OBJECT_TO_CHANNEL = {"whatsapp_business_account": "whatsapp", "page": "messenger", "instagram": "instagram"}

_TEMPLATE_SYNC_STALE_HOURS = 25  # el cron de sync es horario -- más de esto sin sync es señal real


class DebugTokenData(BaseModel):
    """Forma del JSON de `debug_token` NO verificada contra doc primaria de
    2026 -- se parsea defensivo, todos los campos opcionales."""

    model_config = ConfigDict(extra="allow")

    is_valid: bool | None = None
    expires_at: int | None = None  # 0 = "no expira" (regla explícita del ticket)
    scopes: list[str] | None = None


async def check_token_valid(
    token: str, app_id: str, app_secret: str, version: str
) -> PreflightCheck:
    """`GET /debug_token?input_token={token}&access_token={app_id}|{app_secret}`
    -- el `access_token` de ESTA llamada es el token de LA APP
    (`app_id|app_secret`), no el token que se está inspeccionando, así que
    no calza con `graph_fetch` (que pone Bearer + appsecret_proof sobre el
    MISMO token que envía). Va con httpx directo, mismo criterio que
    `meta_preflight.py::_fetch_app_token`."""
    now = datetime.now(UTC)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                f"{GRAPH_BASE}/{version}/debug_token",
                params={"input_token": token, "access_token": f"{app_id}|{app_secret}"},
            )
    except httpx.HTTPError as exc:
        return PreflightCheck(key="token_valid", ok=None, detail=f"No se pudo contactar a Meta: {exc}", checked_at=now)

    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = None
        exc = classify_and_build_error(http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text)
        return PreflightCheck(
            key="token_valid", ok=False, detail=exc.message, checked_at=now, repair_action="reauthorize"
        )

    raw = ((response.json() or {}).get("data")) or {}
    data = DebugTokenData.model_validate(raw)

    from core.shared.alerts import send_alert

    if data.is_valid is False:
        await send_alert(
            "Token de Meta inválido", "debug_token.is_valid=false -- la conexión pasa a degraded, requiere reautorización.",
            severity="critical",
        )
        return PreflightCheck(
            key="token_valid", ok=False, detail="Meta reporta el token como inválido -- requiere reautorización.",
            checked_at=now, repair_action="reauthorize",
        )
    if data.is_valid is None:
        return PreflightCheck(key="token_valid", ok=None, detail="Meta no informó is_valid", checked_at=now)

    if data.expires_at is not None and data.expires_at != 0:
        expires_dt = datetime.fromtimestamp(data.expires_at, tz=UTC)
        if expires_dt < now + timedelta(days=7):
            await send_alert(
                "Token de Meta por vencer",
                f"Vence el {expires_dt.date().isoformat()} (menos de 7 días) -- se encoló refresco, avisar al tenant.",
                severity="warning",
            )
            return PreflightCheck(
                key="token_valid", ok=False,
                detail=f"El token vence el {expires_dt.date().isoformat()} (menos de 7 días).",
                checked_at=now, repair_action="refresh_token",
            )
    return PreflightCheck(key="token_valid", ok=True, detail="Token válido", checked_at=now)


async def check_webhook_subscribed(
    waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> PreflightCheck:
    now = datetime.now(UTC)
    try:
        data = await graph_fetch(
            f"/{waba_id}/subscribed_apps", token=token, app_secret=app_secret, version=version, connection_id=connection_id
        )
    except GraphError as exc:
        return PreflightCheck(key="webhook_subscribed", ok=False, detail=exc.message, checked_at=now, repair_action="subscribe_waba")

    if not (data or {}).get("data"):
        return PreflightCheck(
            key="webhook_subscribed", ok=False,
            detail="La app no está suscripta a esta WABA -- los mensajes no van a llegar.",
            checked_at=now, repair_action="subscribe_waba",
        )
    return PreflightCheck(key="webhook_subscribed", ok=True, detail="Suscripción de WABA activa", checked_at=now)


async def check_app_subscription(connection: MetaConnection) -> PreflightCheck:
    """Delegado: NO vuelve a pegarle a `GET /{app_id}/subscriptions` --
    lee el estado ya persistido por el cron de 15 min
    (`core/tasks/meta_subscriptions_health.py`), a través de la API
    pública de `meta_subscriptions.py` (nunca importa el modelo directo --
    mismo criterio de aislamiento por módulo dueño que ya existe para
    otras tablas de sistema)."""
    from core.gateway.meta_subscriptions import list_app_subscriptions

    now = datetime.now(UTC)
    object_ = next((obj for obj, channel in _OBJECT_TO_CHANNEL.items() if channel == connection.channel), None)
    if object_ is None:
        return PreflightCheck(key="app_subscription_active", ok=None, detail="Canal sin objeto de suscripción conocido", checked_at=now)

    statuses = await list_app_subscriptions(connection.meta_app_id)
    row = next((s for s in statuses if s.object == object_), None)

    if row is None or row.active is None:
        return PreflightCheck(
            key="app_subscription_active", ok=None,
            detail="Todavía no corrió el chequeo de suscripción de app (cada 15 min)", checked_at=now,
        )
    if row.active is False:
        return PreflightCheck(
            key="app_subscription_active", ok=False,
            detail=row.last_error or "Meta deshabilitó la suscripción de la app por fallos repetidos.",
            checked_at=now, repair_action="resubscribe_app",
        )
    return PreflightCheck(key="app_subscription_active", ok=True, detail="Suscripción de app activa", checked_at=now)


async def check_number_registered(
    phone_number_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> PreflightCheck:
    now = datetime.now(UTC)
    try:
        data = await graph_fetch(
            f"/{phone_number_id}", token=token, app_secret=app_secret,
            query={"fields": _PHONE_FIELDS}, version=version, connection_id=connection_id,
        )
    except GraphError as exc:
        return PreflightCheck(key="number_registered", ok=False, detail=exc.message, checked_at=now)

    quality = (data or {}).get("quality_rating")
    verification = (data or {}).get("code_verification_status")

    if quality == "RED":
        from core.shared.alerts import send_alert

        await send_alert(
            "Calidad de número en RED",
            f"phone_number_id={phone_number_id} tiene quality_rating=RED -- riesgo de bloqueo por Meta.",
            severity="critical",
        )

    if verification not in ("VERIFIED", None):
        return PreflightCheck(
            key="number_registered", ok=False,
            detail=f"code_verification_status={verification!r} -- el número no está verificado.", checked_at=now,
        )
    detail = f"quality_rating={quality or 'desconocida'}"
    return PreflightCheck(key="number_registered", ok=True, detail=detail, checked_at=now)


async def check_webhook_override(
    phone_number_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> PreflightCheck:
    """`GET /{PHONE_NUMBER_ID}?fields=webhook_configuration` -- SIEMPRE
    corre (el override es un dato efímero del lado de Meta, no se persiste
    localmente, así que no hay un flag propio del que depender)."""
    now = datetime.now(UTC)
    try:
        data = await graph_fetch(
            f"/{phone_number_id}", token=token, app_secret=app_secret,
            query={"fields": "webhook_configuration"}, version=version, connection_id=connection_id,
        )
    except GraphError as exc:
        return PreflightCheck(key="webhook_override", ok=None, detail=exc.message, checked_at=now)

    config = (data or {}).get("webhook_configuration") or {}
    override_url = config.get("override_callback_uri") or config.get("application")
    if not override_url:
        return PreflightCheck(key="webhook_override", ok=True, detail="Sin override configurado", checked_at=now)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(override_url)
        reachable = response.status_code < 500
    except httpx.HTTPError:
        reachable = False

    if not reachable:
        return PreflightCheck(
            key="webhook_override", ok=False,
            detail=f"El override de webhook apunta a una URL que no responde: {override_url}", checked_at=now,
        )
    return PreflightCheck(key="webhook_override", ok=True, detail=f"Override activo: {override_url}", checked_at=now)


async def check_templates(waba_account_id: uuid.UUID, tenant_id: uuid.UUID) -> PreflightCheck:
    """No pega a Graph -- cuenta localmente por status + chequea antigüedad
    de `synced_at` (el cron de sync ya es horario)."""
    now = datetime.now(UTC)
    async with get_session() as session:
        rows = (
            await session.execute(
                select(MessageTemplate.status, MessageTemplate.synced_at).where(
                    MessageTemplate.waba_account_id == waba_account_id, MessageTemplate.tenant_id == tenant_id
                )
            )
        ).all()

    if not rows:
        return PreflightCheck(key="templates", ok=None, detail="Sin plantillas sincronizadas todavía", checked_at=now)

    counts: dict[str, int] = {}
    newest_sync = None
    for status, synced_at in rows:
        counts[status] = counts.get(status, 0) + 1
        if synced_at and (newest_sync is None or synced_at > newest_sync):
            newest_sync = synced_at

    stale = newest_sync is None or (now - newest_sync) > timedelta(hours=_TEMPLATE_SYNC_STALE_HOURS)
    problematic = counts.get("REJECTED", 0) + counts.get("DISABLED", 0)
    detail = f"{sum(counts.values())} plantillas ({counts}); último sync: {newest_sync.isoformat() if newest_sync else 'nunca'}"

    if stale:
        return PreflightCheck(
            key="templates", ok=False, detail=f"{detail} -- desincronizado hace más de {_TEMPLATE_SYNC_STALE_HOURS}h",
            checked_at=now, repair_action="sync_templates",
        )
    if problematic:
        return PreflightCheck(key="templates", ok=False, detail=f"{detail} -- {problematic} rechazadas/deshabilitadas", checked_at=now)
    return PreflightCheck(key="templates", ok=True, detail=detail, checked_at=now)


def check_billing(connection: MetaConnection) -> PreflightCheck:
    """NO HAY API -- estado manual persistido en `health['billing']
    ['manual_status']` (un admin lo marca a mano en el panel). `ok=None`
    SIEMPRE, tri-estado real, nunca se infiere de otra señal."""
    manual_status = ((connection.health or {}).get("billing") or {}).get("manual_status")
    detail = f"Estado manual: {manual_status or 'sin marcar'} -- Meta no tiene API para esto."
    return PreflightCheck(
        key="billing", ok=None, detail=detail, checked_at=datetime.now(UTC), repair_action="configure_billing"
    )


async def run_health_checks(connection: MetaConnection, tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Resuelve WABA/números/token UNA sola vez, corre los checks que
    aplican, devuelve el dict listo para `MetaConnection.health`."""
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for

    checks: dict[str, PreflightCheck] = {}

    async with get_session() as session:
        waba = (
            await session.execute(
                select(WabaAccount).where(WabaAccount.connection_id == connection.id, WabaAccount.tenant_id == tenant_id)
            )
        ).scalars().first()
        phone_numbers = (
            await session.execute(
                select(PhoneNumber).where(PhoneNumber.connection_id == connection.id, PhoneNumber.tenant_id == tenant_id)
            )
        ).scalars().all()

    # Credenciales de ESTA conexión según su origen (HU-CONN-01): antes el secreto
    # salía de "la" conexión del tenant, que con varias podía ser otra.
    try:
        creds = await credentials_for(connection, waba_id=waba.waba_id if waba is not None else None)
    except CredentialsUnavailable:
        creds = None

    checks["app_subscription_active"] = await check_app_subscription(connection)
    checks["billing"] = check_billing(connection)

    if creds is None:
        return {key: check.model_dump(mode="json") for key, check in checks.items()}

    checks["token_valid"] = await check_token_valid(
        creds.access_token, creds.app_id, creds.app_secret, creds.graph_api_version
    )

    if waba is not None:
        checks["webhook_subscribed"] = await check_webhook_subscribed(
            waba.waba_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
        )
        checks["templates"] = await check_templates(waba.id, tenant_id)

    for phone in phone_numbers:
        suffix = f":{phone.phone_number_id}" if len(phone_numbers) > 1 else ""
        number_check = await check_number_registered(
            phone.phone_number_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
        )
        override_check = await check_webhook_override(
            phone.phone_number_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
        )
        checks[f"number_registered{suffix}"] = number_check
        checks[f"webhook_override{suffix}"] = override_check

    return {key: check.model_dump(mode="json") for key, check in checks.items()}


_HIGH_SEVERITY_KEYS = {"token_valid", "app_subscription_active"}


def decide_status(checks: dict[str, dict[str, Any]]) -> tuple[str, str | None]:
    """`error` solo por los dos checks de severidad alta explícita del
    ticket; `degraded` por cualquier otro fallo; `connected` si todos ok
    (o `None`, no aplica)."""
    failing = [key for key, check in checks.items() if check.get("ok") is False]
    if not failing:
        return "connected", None
    if any(key in _HIGH_SEVERITY_KEYS for key in failing):
        high = [k for k in failing if k in _HIGH_SEVERITY_KEYS]
        return "error", f"Checks críticos fallando: {', '.join(sorted(high))}"
    return "degraded", f"Checks fallando: {', '.join(sorted(failing))}"

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
from collections.abc import Sequence
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
    token: str,
    app_id: str,
    app_secret: str,
    version: str,
    *,
    connection_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> PreflightCheck:
    """`GET /debug_token?input_token={token}&access_token={app_id}|{app_secret}`
    -- el `access_token` de ESTA llamada es el token de LA APP
    (`app_id|app_secret`), no el token que se está inspeccionando, así que
    no calza con `graph_fetch` (que pone Bearer + appsecret_proof sobre el
    MISMO token que envía). Va con httpx directo, mismo criterio que
    `meta_preflight.py::_fetch_app_token`.

    Alerta solo si recibe `connection_id`, una vez al día por conexión (ADR-0102): el
    cron la revisa cada hora, también en `error`. Sin conexión (el wizard manual, antes
    de crearla) no alerta: el admin ya ve el error en pantalla."""
    now = datetime.now(UTC)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                f"{GRAPH_BASE}/{version}/debug_token",
                params={"input_token": token, "access_token": f"{app_id}|{app_secret}"},
            )
    except httpx.HTTPError as exc:
        # Solo el tipo: el texto de la excepción no se guarda ni se muestra al negocio.
        return PreflightCheck(
            key="token_valid", ok=None, detail=f"No se pudo contactar a Meta ({type(exc).__name__})", checked_at=now
        )

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

    from core.shared.alerts import send_alert_once

    conexion = f"La conexión {connection_id} (tenant {tenant_id})"

    if data.is_valid is False:
        if connection_id is not None:
            await send_alert_once(
                f"meta_token_invalido:{connection_id}",
                "Token de Meta inválido",
                f"{conexion}: debug_token.is_valid=false -- la conexión pasa a error, requiere reautorización.",
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
            if connection_id is not None:
                await send_alert_once(
                    f"meta_token_por_vencer:{connection_id}",
                    "Token de Meta por vencer",
                    f"{conexion}: vence el {expires_dt.date().isoformat()} (menos de 7 días) -- "
                    "se encoló refresco, avisar al tenant.",
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

    # La app que de verdad usa la conexión (ADR-0094): con Embedded Signup o carga manual, la
    # de la plataforma. Una BYOK tomada por Embedded Signup conserva su `meta_app_id` viejo
    # (es el `subject_id` de su secreto, ADR-0126 §8); mirar esa app daba la suscripción de
    # una app que la conexión ya no usa (HU-SH-05).
    from core.config import get_settings
    from core.gateway.meta_credentials import uses_platform_app

    app_id = (get_settings().meta_app_id or connection.meta_app_id) if uses_platform_app(connection) else connection.meta_app_id
    statuses = await list_app_subscriptions(app_id)
    row = next((s for s in statuses if s.object == object_), None)

    if row is None or row.active is None:
        return PreflightCheck(
            key="app_subscription_active", ok=None,
            detail=(
                "Todavía no se leyó la suscripción de la app de la plataforma (el cron la lee cada 15 min; "
                "si falta, la registra el admin de plataforma)."
                if uses_platform_app(connection)
                else "Todavía no corrió el chequeo de suscripción de app (cada 15 min)"
            ),
            checked_at=now,
        )
    if row.active is False:
        return PreflightCheck(
            key="app_subscription_active", ok=False,
            detail=row.last_error or "Meta deshabilitó la suscripción de la app por fallos repetidos.",
            checked_at=now, repair_action="resubscribe_app",
        )
    return PreflightCheck(key="app_subscription_active", ok=True, detail="Suscripción de app activa", checked_at=now)


async def check_number_registered(
    phone_number_id: str,
    token: str,
    app_secret: str,
    version: str,
    connection_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
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
        from core.shared.alerts import send_alert_once

        # Una vez al día por conexión y número: el cron lo revisa cada hora (ADR-0102). Con
        # la conexión en la clave, si el número cambia de negocio el nuevo dueño también se entera.
        await send_alert_once(
            f"meta_calidad_red:{connection_id}:{phone_number_id}",
            "Calidad de número en RED",
            f"phone_number_id={phone_number_id} de la conexión {connection_id} (tenant {tenant_id}) tiene "
            "quality_rating=RED -- riesgo de bloqueo por Meta.",
            severity="critical",
        )

    # `status` (registro en la Cloud API) es lo que decide si el número puede enviar.
    # `code_verification_status` solo dice si se validó con código SMS/voz: un número de
    # prueba de Meta, o uno registrado sin código, queda NOT_VERIFIED y envía igual
    # (verificado en test el 2026-10-08). Sin `status` (respuestas viejas) se usa la
    # regla anterior.
    status = (data or {}).get("status")
    detail = f"quality_rating={quality or 'desconocida'}, code_verification_status={verification or 'desconocido'}"
    if status is not None:
        if status != "CONNECTED":
            return PreflightCheck(
                key="number_registered", ok=False,
                detail=f"status={status!r} -- el número no está conectado a la Cloud API.", checked_at=now,
            )
        return PreflightCheck(key="number_registered", ok=True, detail=f"status=CONNECTED, {detail}", checked_at=now)
    if verification not in ("VERIFIED", None):
        return PreflightCheck(
            key="number_registered", ok=False,
            detail=f"code_verification_status={verification!r} -- el número no está verificado.", checked_at=now,
        )
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

    # La URL del override la controla el negocio (su app o su número): se pide solo a un host
    # público por HTTPS, conectando a la IP ya validada y sin redirects (anti-SSRF, ADR-0052;
    # HU-CONN-07 lo deja disparar a demanda desde el checklist de producción).
    from core.mcp.types import ScopedHttp

    reachable = False
    try:
        target = httpx.URL(str(override_url))
        host = target.host
    except Exception:
        target, host = None, ""
    if target is not None and target.scheme == "https" and host:
        result = await ScopedHttp([host], timeout=5.0, methods=("GET",)).request("GET", str(override_url))
        status = result.get("status")
        reachable = isinstance(status, int) and status < 500

    if not reachable:
        return PreflightCheck(
            key="webhook_override", ok=False,
            detail=f"El override de webhook apunta a una URL que no responde o no es pública (HTTPS): {override_url}",
            checked_at=now,
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


# Sin estas credenciales propias una conexión BYOK no es la credencial de nadie (ADR-0094).
_BYOK_SIN_CREDENCIALES = {"missing_app_secret", "missing_access_token"}


def _token_sin_credenciales(connection: MetaConnection, exc: Exception | None) -> PreflightCheck:
    """`token_valid` cuando no hay credenciales para revisarlo (ADR-0102).

    - BYOK sin App Secret o sin token propios: `ok=False`. Nunca es legítimo, y así baja a
      `error` un alta fallida que un "Diagnosticar" anterior dejó en `connected`, que si no
      le seguiría ganando el secreto a la conexión sana.
    - El resto (Embedded Signup o manual con la app de la plataforma sin configurar, un alta
      manual a medias): `ok=None`. No empeora el estado, pero tampoco lo deja mejorar
      (`next_status`), y el panel muestra por qué.
    """
    reason = getattr(exc, "reason", None)
    message = getattr(exc, "message", None) or "Sin credenciales"
    if connection.origin == "byok" and reason in _BYOK_SIN_CREDENCIALES:
        return PreflightCheck(
            key="token_valid", ok=False, detail=f"Faltan las credenciales propias: {message}",
            checked_at=datetime.now(UTC), repair_action="reauthorize",
        )
    return PreflightCheck(
        key="token_valid", ok=None, detail=f"No se pudo revisar: {message}",
        checked_at=datetime.now(UTC), repair_action="reauthorize",
    )


OWN_APP_SUBSCRIBED = "own_app_subscribed"


def check_own_app_subscribed(wabas: Sequence[WabaAccount]) -> PreflightCheck:
    """¿La WABA quedó suscrita a la app de ESTA conexión? Solo BYOK (ADR-0102).

    `webhook_subscribed` solo pide que la WABA tenga alguna app suscrita: un reintento del
    wizard con otra app que falló al suscribir lo pasa, porque la vieja sigue suscrita. Lo
    sabemos por `WabaAccount.subscribed`: el alta BYOK lo pone en `False` al empezar y en
    `True` después de un `POST /subscribed_apps` correcto, y "Resuscribir la WABA" lo
    actualiza.

    - Alguna WABA suscrita: `True`.
    - WABAs, pero ninguna suscrita: `False`, de severidad alta, así que la conexión queda en
      `error`: un reintento fallido que ya estaba en `connected` baja al último nivel de
      `_resolve_byok_token` y la sana gana tanto si está `connected` como `degraded` (una
      plantilla rechazada basta). En `degraded` empataría con una sana degradada y ganaría
      el reintento, por más nuevo. Sin la WABA suscrita, además, no le llegan mensajes.
    - Sin WABA cargada (las BYOK anteriores al post-mortem de ADR-0036): `None`. No degrada,
      pero tampoco deja mejorar; se arregla repitiendo el wizard."""
    now = datetime.now(UTC)
    if any(waba.subscribed for waba in wabas):
        return PreflightCheck(key=OWN_APP_SUBSCRIBED, ok=True, detail="La WABA está suscrita a esta app", checked_at=now)
    if wabas:
        return PreflightCheck(
            key=OWN_APP_SUBSCRIBED, ok=False,
            detail="La WABA no quedó suscrita a la app de esta conexión: resuscribila.",
            checked_at=now, repair_action="subscribe_waba",
        )
    return PreflightCheck(
        key=OWN_APP_SUBSCRIBED, ok=None,
        detail="La conexión no tiene la WABA cargada: repetí el wizard BYOK.",
        checked_at=now, repair_action="reauthorize",
    )


async def run_health_checks(connection: MetaConnection, tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Resuelve WABA/números/token UNA sola vez, corre los checks que
    aplican, devuelve el dict listo para `MetaConnection.health`."""
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for

    checks: dict[str, PreflightCheck] = {}

    async with get_session() as session:
        wabas = (
            await session.execute(
                select(WabaAccount).where(WabaAccount.connection_id == connection.id, WabaAccount.tenant_id == tenant_id)
            )
        ).scalars().all()
        waba = wabas[0] if wabas else None
        phone_numbers = (
            await session.execute(
                select(PhoneNumber).where(PhoneNumber.connection_id == connection.id, PhoneNumber.tenant_id == tenant_id)
            )
        ).scalars().all()

    # Credenciales de ESTA conexión según su origen (HU-CONN-01): antes el secreto
    # salía de "la" conexión del tenant, que con varias podía ser otra.
    sin_credenciales: CredentialsUnavailable | None = None
    try:
        creds = await credentials_for(connection, waba_id=waba.waba_id if waba is not None else None)
    except CredentialsUnavailable as exc:
        creds = None
        sin_credenciales = exc

    checks["app_subscription_active"] = await check_app_subscription(connection)
    checks["billing"] = check_billing(connection)

    if creds is None:
        checks["token_valid"] = _token_sin_credenciales(connection, sin_credenciales)
        return {key: check.model_dump(mode="json") for key, check in checks.items()}

    checks["token_valid"] = await check_token_valid(
        creds.access_token, creds.app_id, creds.app_secret, creds.graph_api_version,
        connection_id=connection.id, tenant_id=tenant_id,
    )

    if waba is not None:
        checks["webhook_subscribed"] = await check_webhook_subscribed(
            waba.waba_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
        )
        checks["templates"] = await check_templates(waba.id, tenant_id)
    if connection.origin == "byok":
        checks[OWN_APP_SUBSCRIBED] = check_own_app_subscribed(wabas)

    for phone in phone_numbers:
        suffix = f":{phone.phone_number_id}" if len(phone_numbers) > 1 else ""
        number_check = await check_number_registered(
            phone.phone_number_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id,
            tenant_id=tenant_id,
        )
        override_check = await check_webhook_override(
            phone.phone_number_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
        )
        checks[f"number_registered{suffix}"] = number_check
        checks[f"webhook_override{suffix}"] = override_check

    return {key: check.model_dump(mode="json") for key, check in checks.items()}


# `own_app_subscribed` (ADR-0102): sin la WABA suscrita a su app no le llegan mensajes, y
# un reintento fallido en `degraded` podría ganarle el secreto a una sana degradada.
_HIGH_SEVERITY_KEYS = {"token_valid", "app_subscription_active", OWN_APP_SUBSCRIBED}


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


# Rango de cada estado para `next_status`: mejorar es bajar de rango. `pending` (un alta a
# medias) no está verificada: pasar a `connected` cuenta como mejora. `revoked`/`draft` no
# pasan por acá; si llegaran, no cambian.
_RANGO = {"connected": 0, "pending": 1, "degraded": 1, "error": 2}


def next_status(
    current: str, current_reason: str | None, origin: str, checks: dict[str, dict[str, Any]]
) -> tuple[str, str | None]:
    """Estado que queda después de un diagnóstico (ADR-0102).

    Empeorar siempre se puede. Mejorar (`error` → `degraded` → `connected`) solo con el
    diagnóstico verificado:
    - el token se verificó contra Meta (`token_valid` en `True`). Sin credenciales,
      `run_health_checks` no lo puede revisar y `decide_status` daría `connected`: un alta
      fallida pasaría a ser la conexión sana y le quitaría el secreto a la de verdad;
    - en BYOK, además, SU app está suscrita: la suscripción de la app confirmada
      (`app_subscription_active`) y la WABA suscrita a esa app (`own_app_subscribed`). Un
      reintento del wizard con otra app que falló al suscribir tiene token válido, y la WABA
      sigue suscrita a la app vieja, que es la que firma.

    Si no se cumple, quedan el estado y la razón actuales. Con un estado que no conoce
    tampoco cambia nada: falla cerrado.
    """
    rango_actual = _RANGO.get(current)
    if rango_actual is None:
        return current, current_reason
    new_status, reason = decide_status(checks)
    if _RANGO[new_status] >= rango_actual:
        return new_status, reason

    verificado = checks.get("token_valid", {}).get("ok") is True
    if origin == "embedded_signup":
        # La app de la plataforma es la que recibe sus mensajes: sin su suscripción confirmada
        # (`None` porque nadie la leyó cuenta como no confirmada) no mejora (HU-SH-05, ADR-0128).
        verificado = verificado and checks.get("app_subscription_active", {}).get("ok") is True
    if origin == "byok":
        verificado = (
            verificado
            and checks.get("app_subscription_active", {}).get("ok") is True
            and checks.get(OWN_APP_SUBSCRIBED, {}).get("ok") is True
        )
    if not verificado:
        return current, current_reason
    return new_status, reason


def misma_conexion(snapshot: MetaConnection, locked: MetaConnection) -> bool:
    """¿La fila bloqueada sigue siendo la conexión que se diagnosticó? (ADR-0102)

    Si el wizard la reconectó durante el diagnóstico (otro origen, otra app, u otro
    `connected_at`), los checks son de credenciales que ya no usa: se descartan."""
    return (snapshot.origin, snapshot.meta_app_id, snapshot.connected_at) == (
        locked.origin,
        locked.meta_app_id,
        locked.connected_at,
    )


# Claves de `health` que no son checks: las pone el alta, y el diagnóstico, que reemplaza
# el JSON, no debe borrarlas (ADR-0102).
_NO_SON_CHECKS = ("coexistence", "token_scope_check", "webhook_subscription_active")


# Orden en que se muestran los checks: primero los que cortan la conexión. Postgres no
# conserva el orden de las claves de un `jsonb`.
_ORDEN_DE_CHECKS = (
    "token_valid",
    "app_subscription_active",
    OWN_APP_SUBSCRIBED,
    "webhook_subscribed",
    "number_registered",
    "webhook_override",
    "templates",
    "billing",
)


def _orden(key: str) -> tuple[int, str]:
    base = key.split(":", 1)[0]  # `number_registered:<phone_number_id>` con varios números
    return (_ORDEN_DE_CHECKS.index(base) if base in _ORDEN_DE_CHECKS else len(_ORDEN_DE_CHECKS), key)


def checks_de_salud(health: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Los checks de `health`, en orden y sin lo que no es un check (HU-CONN-04).

    `health` guarda también datos del alta (`_NO_SON_CHECKS`), y el panel los listaba como
    chequeos. Un check es un objeto con `ok`; `billing` lleva además el `manual_status`."""
    checks = []
    for key in sorted((health or {}).keys(), key=_orden):
        value = (health or {})[key]
        if key in _NO_SON_CHECKS or not isinstance(value, dict) or "ok" not in value:
            continue
        checks.append(
            {
                "key": key,
                "ok": value.get("ok"),
                "detail": value.get("detail"),
                "repair_action": value.get("repair_action"),
                "checked_at": value.get("checked_at"),
            }
        )
    return checks


def merge_health(previous: dict[str, Any] | None, checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`health` nuevo: los checks del diagnóstico más lo que no es un check.

    - `coexistence`: lo pone Embedded Signup, y `meta_onboarding` lo usa para no registrar
      un número que comparte la app de WhatsApp Business.
    - `token_scope_check` y `webhook_subscription_active`: datos de soporte del alta.
    - `billing.manual_status`: lo marca un admin a mano (Meta no tiene API).
    """
    previous = previous or {}
    merged: dict[str, Any] = dict(checks)
    for key in _NO_SON_CHECKS:
        if key in previous:
            merged[key] = previous[key]
    manual_status = (previous.get("billing") or {}).get("manual_status")
    if manual_status is not None:
        merged["billing"] = {**(merged.get("billing") or {}), "manual_status": manual_status}
    return merged

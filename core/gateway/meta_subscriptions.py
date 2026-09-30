"""Registro y verificación de suscripciones a nivel de APP de Meta
(`POST /{app_id}/subscriptions`).

Distinto de `core/gateway/webhooks.py` (recepción) y de la suscripción por
objeto individual (`WabaAccount.subscribed`/`FbPage.subscribed_fields`,
`POST /{waba_id}/subscribed_apps`): esto es la config de webhook de la app
como un todo -- sin esto, ningún webhook de ningún tenant que use esa app
llega nunca, sin importar que sus objetos individuales estén bien suscritos.

Ver ADR-0029 (por qué `meta_app_subscriptions` es tabla de sistema sin RLS).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import MetaAppSubscription, utcnow
from core.db.session import get_session
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_preflight import AppCredentials
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
_TIMEOUT_S = 15.0

# Conjuntos de fields por objeto, exactos -- no se derivan ni se completan
# con "todos los que existan": suscribir un field de más es una superficie
# de datos que nadie pidió.
OBJECT_FIELDS: dict[str, list[str]] = {
    "whatsapp_business_account": [
        "messages",
        "message_template_status_update",
        "message_template_quality_update",
        "template_category_update",
        "phone_number_quality_update",
        "phone_number_name_update",
        "account_update",
        "account_alerts",
    ],
    "page": [
        "messages",
        "messaging_postbacks",
        "messaging_optins",
        "message_deliveries",
        "message_reads",
        "messaging_referrals",
        "message_echoes",
        "messaging_handovers",
    ],
    "instagram": [
        "messages",
        "messaging_postbacks",
        "messaging_seen",
        "message_reactions",
        "messaging_referral",
        "comments",
        "mentions",
    ],
}

# object (Graph API) -> MetaConnection.channel (mismo mapeo que
# core/gateway/webhook_flatten.py / webhook_route_tenant.py del PR #42).
OBJECT_TO_CHANNEL: dict[str, str] = {
    "whatsapp_business_account": "whatsapp",
    "page": "messenger",
    "instagram": "instagram",
}


class SubscriptionError(AgentCoreError):
    """Error de esta capa (self-check de callback fallido, `object` faltante
    en delete) -- distinto de `GraphError`, que es un error de Graph API en
    sí devuelto por Meta."""


@dataclass
class AppSubscriptionStatus:
    """Vista de una fila de `meta_app_subscriptions`, segura para exponer en
    JSON/plantillas -- nunca lleva `app_secret` ni `verify_token`."""

    meta_app_id: str
    object: str
    callback_url: str | None
    active: bool | None
    fields: list[str]
    last_checked_at: datetime | None
    last_error: str | None


def _to_status(row: MetaAppSubscription) -> AppSubscriptionStatus:
    return AppSubscriptionStatus(
        meta_app_id=row.meta_app_id,
        object=row.object,
        callback_url=row.callback_url,
        active=row.active,
        fields=list(row.fields or []),
        last_checked_at=row.last_checked_at,
        last_error=row.last_error,
    )


async def fetch_app_access_token(app_id: str, app_secret: str, version: str) -> str:
    """Igual patrón que `meta_preflight.py::_fetch_app_token` (duplicado a
    propósito, ~15 líneas -- no vale el riesgo de refactorizar ese módulo ya
    mergeado en producción por compartir esto). No pasa por `graph_fetch`:
    el intercambio `client_credentials` no lleva Authorization Bearer ni
    `appsecret_proof` (todavía no hay token sobre el que calcularlo)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                f"{GRAPH_BASE}/{version}/oauth/access_token",
                params={"client_id": app_id, "client_secret": app_secret, "grant_type": "client_credentials"},
            )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise SubscriptionError(f"No se pudo contactar a Meta para obtener el token de app: {exc}") from exc

    if response.status_code >= 400:
        from core.gateway.meta_errors import classify_and_build_error

        try:
            body = response.json()
        except Exception:
            body = None
        raise classify_and_build_error(
            http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text
        )

    try:
        return response.json().get("access_token") or f"{app_id}|{app_secret}"
    except Exception:
        return f"{app_id}|{app_secret}"


async def _self_check_callback(callback_url: str, verify_token: str) -> None:
    """Gate duro ANTES de pedirle a Meta que registre la suscripción: si
    nuestro propio handshake GET no responde el challenge exacto, Meta va a
    devolver un 422 opaco ("Webhook callback URL is not reachable or
    invalid") -- mejor abortar acá con un mensaje claro (criterio de
    aceptación explícito del ticket). Mismo patrón que
    `meta_preflight.py::_check_webhook_handshake`, pero acá levanta en vez
    de devolver un check informativo."""
    if not verify_token:
        raise SubscriptionError("No hay verify_token configurado (ni en la conexión ni en .env)")

    challenge = "meta-subscriptions-self-check"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                callback_url,
                params={"hub.mode": "subscribe", "hub.challenge": challenge, "hub.verify_token": verify_token},
            )
    except httpx.TimeoutException as exc:
        raise SubscriptionError(
            f"Timeout al probar el propio callback_url ({callback_url}). "
            "Un cold start más lento que el timeout de Meta produciría el mismo síntoma en producción."
        ) from exc
    except httpx.ConnectError as exc:
        raise SubscriptionError(
            f"No se pudo conectar a {callback_url} ({exc}). "
            "¿La URL pública (PUBLIC_BASE_URL) está bien configurada y expuesta?"
        ) from exc

    if response.status_code != 200 or response.text != challenge:
        raise SubscriptionError(
            f"El propio callback_url respondió {response.status_code} en vez de 200 con el challenge "
            "exacto -- Meta rechazaría esto con un 422 opaco. Revisá que la ruta no esté detrás de "
            "ningún middleware/proxy que intercepte el handshake antes de llegar al handler."
        )


def humanize_subscription_error(exc: GraphError) -> str:
    """Traduce un `GraphError` ya clasificado a un mensaje de usuario, por
    SUBSTRING del mensaje que devuelve Meta (nunca por código numérico
    inventado -- no hay tabla oficial de códigos verificada para este
    endpoint en particular, solo la clasificación real que ya hace
    `meta_errors.py` a partir de lo que la respuesta trae)."""
    text = f"{exc.message} {exc.error_user_msg or ''}".lower()

    # Orden importa: "not reachable" es más específico que el chequeo genérico
    # de formato de URI/URL de más abajo (el propio mensaje de "no reachable"
    # de Meta también contiene la palabra "url").
    if "callback" in text and "reachable" in text:
        return (
            "Meta no pudo alcanzar el callback_url. Dos causas probables: (1) el endpoint no está "
            "expuesto públicamente todavía (falta túnel/DNS en desarrollo), o (2) el proceso tardó "
            "más que el timeout de Meta en responder (cold start)."
        )
    if "verification failed" in text or "ssl" in text or "curl_errno" in text:
        return (
            "Meta no pudo verificar el webhook: falló la conexión TLS a la URL pública "
            "(SSL/conexión rechazada). Reintentá el registro; si persiste, revisá el túnel/dominio público."
        )
    if "callback" in text and ("uri" in text or "url" in text):
        return "La URL de callback no es válida: revisá que sea HTTPS y que no tenga un error de tipeo."
    if exc.category == "AUTH":
        return "Meta rechazó las credenciales: el App Secret configurado es incorrecto."
    if exc.category == "RATE_LIMIT":
        espera = f"{exc.retry_after_ms // 1000}s" if exc.retry_after_ms else "unos minutos"
        return f"Meta está limitando las llamadas de esta app. Esperá {espera} antes de reintentar."
    return f"Error de Meta: {exc.message}"


async def persist_subscription_state(
    meta_app_id: str,
    object: str,
    *,
    callback_url: str | None,
    active: bool | None,
    fields: list[str],
    last_error: str | None,
) -> MetaAppSubscription:
    async with get_session() as session:
        stmt = (
            pg_insert(MetaAppSubscription)
            .values(
                meta_app_id=meta_app_id,
                object=object,
                callback_url=callback_url,
                active=active,
                fields=fields,
                last_checked_at=utcnow(),
                last_error=last_error,
            )
            .on_conflict_do_update(
                constraint="uq_meta_app_subscriptions_app_object",
                set_={
                    "callback_url": callback_url,
                    "active": active,
                    "fields": fields,
                    "last_checked_at": utcnow(),
                    "last_error": last_error,
                    "updated_at": utcnow(),
                },
            )
            .returning(MetaAppSubscription)
        )
        row = (await session.execute(stmt)).scalar_one()
        await session.commit()
        return row


async def upsert_app_subscription(
    config: AppCredentials, object: str, *, callback_url: str, verify_token: str
) -> AppSubscriptionStatus:
    """Idempotente por `object`: reenviar reemplaza, no duplica -- el wizard
    puede llamarlo sin miedo."""
    if object not in OBJECT_FIELDS:
        raise SubscriptionError(f"object desconocido: '{object}' (válidos: {sorted(OBJECT_FIELDS)})")

    await _self_check_callback(callback_url, verify_token)

    token = await fetch_app_access_token(config.app_id, config.app_secret, config.graph_api_version)
    fields = OBJECT_FIELDS[object]

    try:
        await graph_fetch(
            f"/{config.app_id}/subscriptions",
            method="POST",
            token=token,
            app_secret=config.app_secret,
            query={
                "object": object,
                "callback_url": callback_url,
                "verify_token": verify_token,
                "fields": ",".join(fields),
                "include_values": "true",
            },
            version=config.graph_api_version,
            connection_id=config.connection_id,
        )
    except GraphError as exc:
        await persist_subscription_state(
            config.app_id, object, callback_url=callback_url, active=None, fields=fields,
            last_error=humanize_subscription_error(exc),
        )
        raise

    row = await persist_subscription_state(
        config.app_id, object, callback_url=callback_url, active=True, fields=fields, last_error=None
    )
    return _to_status(row)


async def list_app_subscriptions(meta_app_id: str | None = None) -> list[AppSubscriptionStatus]:
    """Lee el estado persistido (no pega a Meta en cada request de panel --
    el cron de salud lo mantiene fresco; `upsert_app_subscription` es el
    único camino que fuerza un round-trip real)."""
    async with get_session() as session:
        stmt = select(MetaAppSubscription)
        if meta_app_id is not None:
            stmt = stmt.where(MetaAppSubscription.meta_app_id == meta_app_id)
        rows = (await session.execute(stmt)).scalars().all()
    return [_to_status(row) for row in rows]


async def delete_app_subscription(config: AppCredentials, *, object: str) -> None:
    """PROHIBIDO llamar sin `object`: el DELETE de Meta sin ese parámetro
    borra TODAS las suscripciones de la app. `object` es keyword-only sin
    default -- omitirlo ya es un `TypeError` de Python al llamar; el chequeo
    explícito de abajo cubre además el caso `object=""`, que Python no
    bloquea por sí solo."""
    if not object:
        raise ValueError("delete_app_subscription requiere 'object' explícito -- nunca borres TODAS las suscripciones")
    if object not in OBJECT_FIELDS:
        raise SubscriptionError(f"object desconocido: '{object}' (válidos: {sorted(OBJECT_FIELDS)})")

    token = await fetch_app_access_token(config.app_id, config.app_secret, config.graph_api_version)
    await graph_fetch(
        f"/{config.app_id}/subscriptions",
        method="DELETE",
        token=token,
        app_secret=config.app_secret,
        query={"object": object},
        version=config.graph_api_version,
        connection_id=config.connection_id,
    )
    async with get_session() as session:
        await session.execute(
            MetaAppSubscription.__table__.delete().where(
                MetaAppSubscription.meta_app_id == config.app_id, MetaAppSubscription.object == object
            )
        )
        await session.commit()

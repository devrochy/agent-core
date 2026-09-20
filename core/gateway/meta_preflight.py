"""Preflight de la app de Meta: checks automáticos contra Graph API.

Complementa la checklist MANUAL (core/admin/templates/meta_requisitos.html,
pasos que solo un humano puede hacer en developers.facebook.com) con lo que
SÍ se puede verificar por API: credenciales de la app, metadata,
suscripciones de webhook y que el handshake público responda.

`PreflightCheck` nunca lleva secretos (ver test_meta_preflight.py) -- ni el
App Secret ni ningún token aparecen en `detail`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import BaseModel

from core.config import get_settings
from core.gateway.connections import effective_credentials
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import graph_fetch
from core.shared.tenant import tenant_scope

GRAPH_BASE = "https://graph.facebook.com"


@dataclass
class AppCredentials:
    app_id: str
    app_secret: str
    config_id: str
    graph_api_version: str
    connection_id: object | None = None  # uuid.UUID de MetaConnection, si vino de BD


async def resolve_app_credentials(tenant_id, channel: str = "whatsapp") -> AppCredentials | None:
    """`MetaConnection` (metadata) + `meta_token_store` (secreto, `kind='app_access'`)
    -> fallback a .env (dev/single-tenant) -> None. Reemplaza el viejo
    `effective_meta_app_config()` (AppSettings.meta_app_config_encrypted,
    deprecado en la migración 0014): la app de Meta ahora es un `MetaConnection`
    de primera clase, no un blob en AppSettings.

    Filtra por `channel` (default "whatsapp", el único que hoy tiene webhook
    self-test real -- `tenant_webhook_url` es específico de WhatsApp): un
    tenant con conexiones de varios canales/entornos no debe mezclar el
    app_id de Messenger con el check pensado para WhatsApp. Si hay más de una
    conexión para el mismo canal, prioriza `status='connected'` y luego la
    más reciente -- todavía no hay selector de conexión en el panel (fuera de
    alcance de esta tanda), así que esto es la mejor aproximación sin UI."""
    from sqlalchemy import select

    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store

    async with get_session() as session:
        connection = (
            await session.execute(
                select(MetaConnection)
                .where(MetaConnection.tenant_id == tenant_id, MetaConnection.channel == channel)
                .order_by((MetaConnection.status != "connected"), MetaConnection.created_at.desc())
            )
        ).scalars().first()

    if connection is not None:
        app_secret = await meta_token_store.get_token(
            connection.id, "app_access", connection.meta_app_id, tenant_id=tenant_id
        )
        if app_secret:
            return AppCredentials(
                app_id=connection.meta_app_id,
                app_secret=app_secret,
                config_id=connection.meta_config_id or "",
                graph_api_version=connection.graph_api_version,
                connection_id=connection.id,
            )

    settings = get_settings()
    if settings.meta_app_id and settings.meta_app_secret:
        return AppCredentials(
            app_id=settings.meta_app_id,
            app_secret=settings.meta_app_secret,
            config_id=settings.meta_config_id,
            graph_api_version=settings.graph_api_version,
        )
    return None


class PreflightCheck(BaseModel):
    key: str
    ok: bool | None  # None = "unknown" (rate limit u otro motivo no concluyente)
    detail: str
    # Extensión para health checks de conexión (core/gateway/meta_health.py):
    # `checked_at` marca cuándo corrió ESTE check puntual (los 7 checks de
    # salud no corren todos en el mismo instante si alguno se reintenta);
    # `repair_action` es un id corto que la UI mapea a un botón/ruta
    # concreta -- nunca una URL cruda acá, para no acoplar el dominio a
    # rutas del panel.
    checked_at: datetime | None = None
    repair_action: str | None = None


# Pasos MANUALES: nadie los puede automatizar (viven en developers.facebook.com).
# `link_suffix` se concatena a `https://developers.facebook.com/apps/{app_id}/`
# cuando ya hay `app_id` configurado; si no, el link genérico manda a la lista
# de apps (el tenant todavía no tiene una app que enlazar).
MANUAL_STEPS: list[dict[str, str]] = [
    {"key": "crear_app", "label": "Crear la app en Meta for Developers", "link_suffix": ""},
    {"key": "copiar_app_secret", "label": "Copiar el App Secret", "link_suffix": "settings/basic/"},
    {
        "key": "agregar_productos",
        "label": "Añadir productos: WhatsApp, Messenger, Instagram, Facebook Login for Business",
        "link_suffix": "",
    },
    {
        "key": "login_configuration",
        "label": "Crear la Login Configuration y copiar el config_id",
        "link_suffix": "fb-login/settings/",
    },
    {
        "key": "webhook_fields",
        "label": "Marcar los campos de webhook (messages, etc.)",
        "link_suffix": "webhooks/",
    },
    {
        "key": "business_verification",
        "label": "Completar Business Verification",
        "link_suffix": "",
    },
    {"key": "app_review", "label": "Pasar App Review de los permisos necesarios", "link_suffix": "app-review/permissions/"},
    {"key": "live_mode", "label": "Pasar la app a Live Mode", "link_suffix": "settings/basic/"},
]


def _error_to_check(key: str, exc: GraphError) -> PreflightCheck:
    """Traduce un GraphError ya clasificado (core/gateway/meta_errors.py) al
    `ok` tri-estado del preflight: RATE_LIMIT -> unknown (None), AUTH ->
    mensaje explícito de reautorización, el resto -> False."""
    if exc.category == "RATE_LIMIT":
        return PreflightCheck(key=key, ok=None, detail=f"Rate limit de Meta (code {exc.code}): reintentá más tarde")
    if exc.category == "AUTH":
        return PreflightCheck(key=key, ok=False, detail="Meta rechazó las credenciales: revisá META_APP_SECRET")
    return PreflightCheck(key=key, ok=False, detail=f"Error de Meta: {exc.message}")


async def _fetch_app_token(config: AppCredentials, version: str) -> tuple[str | None, PreflightCheck]:
    """(token, check). `token` es None si el check falló -- los checks
    siguientes lo saltan en vez de intentar con un token inválido.

    No pasa por `graph_fetch()`: el intercambio `client_credentials` no lleva
    Authorization Bearer ni `appsecret_proof` (todavía no hay token sobre el
    que calcularlo) -- es la única llamada de este módulo que no calza con
    el cliente único, pero reusa la misma clasificación de errores."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{GRAPH_BASE}/{version}/oauth/access_token",
                params={
                    "client_id": config.app_id,
                    "client_secret": config.app_secret,
                    "grant_type": "client_credentials",
                },
            )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        return None, PreflightCheck(key="app_token", ok=False, detail=f"No se pudo contactar a Meta: {exc}")

    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = None
        error = classify_and_build_error(
            http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text
        )
        return None, _error_to_check("app_token", error)

    try:
        token = response.json().get("access_token") or f"{config.app_id}|{config.app_secret}"
    except Exception:
        token = f"{config.app_id}|{config.app_secret}"
    return token, PreflightCheck(key="app_token", ok=True, detail="Token de app obtenido correctamente")


async def _check_app_info(config: AppCredentials, version: str, token: str) -> PreflightCheck:
    try:
        data = await graph_fetch(
            f"/{config.app_id}",
            token=token,
            app_secret=config.app_secret,
            query={"fields": "name,category,link,app_type"},
            version=version,
            connection_id=config.connection_id,
        )
    except GraphError as exc:
        return _error_to_check("app_info", exc)

    detail = f"{data.get('name', '?')} · {data.get('category', '?')} · {data.get('app_type', '?')}"
    return PreflightCheck(key="app_info", ok=True, detail=detail)


async def _check_subscriptions(config: AppCredentials, version: str, token: str) -> PreflightCheck:
    try:
        data = await graph_fetch(
            f"/{config.app_id}/subscriptions",
            token=token,
            app_secret=config.app_secret,
            version=version,
            connection_id=config.connection_id,
        )
    except GraphError as exc:
        return _error_to_check("app_subscriptions", exc)

    items = (data or {}).get("data", [])
    if not items:
        return PreflightCheck(key="app_subscriptions", ok=False, detail="La app no tiene ninguna suscripción de webhook")

    resumen = "; ".join(
        f"{obj.get('object', '?')}: callback={obj.get('callback_url', '?')} "
        f"active={obj.get('active', False)} fields={len(obj.get('fields', []))}"
        for obj in items
    )
    return PreflightCheck(key="app_subscriptions", ok=True, detail=resumen[:400])


def _check_config_id(config: AppCredentials) -> PreflightCheck:
    if re.fullmatch(r"\d+", config.config_id or ""):
        return PreflightCheck(key="config_id", ok=True, detail="config_id presente y con formato numérico")
    return PreflightCheck(key="config_id", ok=False, detail="META_CONFIG_ID vacío o con formato inválido")


async def _check_webhook_handshake(client: httpx.AsyncClient, tenant_slug: str) -> PreflightCheck:
    from core.tenants import tenant_webhook_url

    verify_token = (await effective_credentials()).get("verify_token", "")
    if not verify_token:
        return PreflightCheck(
            key="webhook_handshake", ok=False, detail="No hay verify_token configurado (ni en la conexión ni en .env)"
        )

    url = tenant_webhook_url(tenant_slug)
    challenge = "12345"
    try:
        response = await client.get(
            url,
            params={"hub.mode": "subscribe", "hub.challenge": challenge, "hub.verify_token": verify_token},
        )
    except httpx.TimeoutException:
        return PreflightCheck(
            key="webhook_handshake",
            ok=False,
            detail="Timeout al llamar al webhook público. Meta exige HTTPS con certificado válido: no acepta autofirmados.",
        )
    except httpx.ConnectError as exc:
        return PreflightCheck(
            key="webhook_handshake",
            ok=False,
            detail=f"No se pudo resolver/conectar al webhook ({exc}). Meta exige HTTPS con certificado válido: no acepta autofirmados.",
        )

    if response.status_code == 200 and response.text == challenge:
        return PreflightCheck(key="webhook_handshake", ok=True, detail="El handshake respondió correctamente")
    return PreflightCheck(
        key="webhook_handshake",
        ok=False,
        detail=f"El webhook respondió {response.status_code} en vez de 200 con el challenge exacto",
    )


async def run_preflight(tenant_id, tenant_slug: str) -> list[PreflightCheck]:
    """Corre todos los checks automáticos. Nunca levanta excepción: un check que
    no se pudo correr (config faltante) se reporta como `ok=False`, no rompe
    el resto de la checklist.

    Envuelve todo en `tenant_scope(tenant_id)`: `resolve_app_credentials` ya
    toma `tenant_id` explícito, pero `_check_webhook_handshake` resuelve el
    `verify_token` vía `effective_credentials()`, que depende del contextvar
    *ambiente* (no de un parámetro). Sin este `tenant_scope`, un caller futuro
    que invoque `run_preflight(tenant_id_de_B, ...)` desde un contexto cuyo
    tenant ambiente siga siendo A probaría el webhook de B con credenciales de
    A -- hallazgo del auditor-multitenant, 2026-09-19."""
    async with tenant_scope(tenant_id):
        config = await resolve_app_credentials(tenant_id)
        version = config.graph_api_version if config else get_settings().graph_api_version

        if config is None:
            return [
                PreflightCheck(key="app_token", ok=False, detail="Meta App no configurada para este tenant"),
                PreflightCheck(key="app_info", ok=False, detail="Meta App no configurada para este tenant"),
                PreflightCheck(key="app_subscriptions", ok=False, detail="Meta App no configurada para este tenant"),
                PreflightCheck(key="config_id", ok=False, detail="META_CONFIG_ID vacío o con formato inválido"),
                await _run_webhook_check_only(tenant_slug),
            ]

        checks: list[PreflightCheck] = []
        token, token_check = await _fetch_app_token(config, version)
        checks.append(token_check)

        if token is not None:
            checks.append(await _check_app_info(config, version, token))
            checks.append(await _check_subscriptions(config, version, token))
        else:
            skip_detail = "Sin token de app válido, no se pudo verificar"
            checks.append(PreflightCheck(key="app_info", ok=token_check.ok, detail=skip_detail))
            checks.append(PreflightCheck(key="app_subscriptions", ok=token_check.ok, detail=skip_detail))

        checks.append(_check_config_id(config))
        async with httpx.AsyncClient(timeout=15) as client:
            checks.append(await _check_webhook_handshake(client, tenant_slug))
        return checks


async def _run_webhook_check_only(tenant_slug: str) -> PreflightCheck:
    async with httpx.AsyncClient(timeout=15) as client:
        return await _check_webhook_handshake(client, tenant_slug)

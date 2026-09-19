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

import httpx
from pydantic import BaseModel

from core.config import get_settings
from core.gateway.connections import effective_credentials
from core.gateway.meta_app_config import MetaAppConfig, effective_meta_app_config
from core.shared.tenant import tenant_scope

GRAPH_BASE = "https://graph.facebook.com"

# Meta: códigos de rate limit / uso excesivo de la app -- no reintentar en
# bucle, reportar el check como "unknown" con el motivo (ver docstring del
# módulo y CLAUDE.md invariante nº7: los datos externos no se ejecutan como
# instrucción, y un rate limit no es un "reintentá ahora").
RATE_LIMIT_CODES = {4, 17, 32, 613, 80001, 80004}
OAUTH_EXCEPTION_CODE = 190


class PreflightCheck(BaseModel):
    key: str
    ok: bool | None  # None = "unknown" (rate limit u otro motivo no concluyente)
    detail: str


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


def _meta_error(response: httpx.Response) -> tuple[int | None, str]:
    """(code, message) del body de error de Meta, sin reventar si no es JSON."""
    try:
        error = response.json().get("error", {})
    except Exception:
        return None, response.text[:200]
    return error.get("code"), str(error.get("message") or "")[:200]


async def _fetch_app_token(
    client: httpx.AsyncClient, config: MetaAppConfig, version: str
) -> tuple[str | None, PreflightCheck]:
    """(token, check). `token` es None si el check falló -- los checks
    siguientes lo saltan en vez de intentar con un token inválido."""
    try:
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
        code, message = _meta_error(response)
        if code == OAUTH_EXCEPTION_CODE:
            return None, PreflightCheck(
                key="app_token", ok=False, detail="Meta rechazó las credenciales: revisá META_APP_SECRET"
            )
        if code in RATE_LIMIT_CODES:
            return None, PreflightCheck(
                key="app_token", ok=None, detail=f"Rate limit de Meta (code {code}): reintentá más tarde"
            )
        return None, PreflightCheck(
            key="app_token", ok=False, detail=f"Error de Meta: {message or response.status_code}"
        )

    try:
        token = response.json().get("access_token") or f"{config.app_id}|{config.app_secret}"
    except Exception:
        token = f"{config.app_id}|{config.app_secret}"
    return token, PreflightCheck(key="app_token", ok=True, detail="Token de app obtenido correctamente")


async def _check_app_info(client: httpx.AsyncClient, config: MetaAppConfig, version: str, token: str) -> PreflightCheck:
    try:
        response = await client.get(
            f"{GRAPH_BASE}/{version}/{config.app_id}",
            params={"fields": "name,category,link,app_type", "access_token": token},
        )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        return PreflightCheck(key="app_info", ok=False, detail=f"No se pudo contactar a Meta: {exc}")

    if response.status_code >= 400:
        code, message = _meta_error(response)
        if code == OAUTH_EXCEPTION_CODE:
            return PreflightCheck(key="app_info", ok=False, detail="Meta rechazó las credenciales: revisá META_APP_SECRET")
        if code in RATE_LIMIT_CODES:
            return PreflightCheck(key="app_info", ok=None, detail=f"Rate limit de Meta (code {code}): reintentá más tarde")
        return PreflightCheck(key="app_info", ok=False, detail=f"Error de Meta: {message or response.status_code}")

    data = response.json()
    detail = f"{data.get('name', '?')} · {data.get('category', '?')} · {data.get('app_type', '?')}"
    return PreflightCheck(key="app_info", ok=True, detail=detail)


async def _check_subscriptions(client: httpx.AsyncClient, config: MetaAppConfig, version: str, token: str) -> PreflightCheck:
    try:
        response = await client.get(
            f"{GRAPH_BASE}/{version}/{config.app_id}/subscriptions",
            params={"access_token": token},
        )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        return PreflightCheck(key="app_subscriptions", ok=False, detail=f"No se pudo contactar a Meta: {exc}")

    if response.status_code >= 400:
        code, message = _meta_error(response)
        if code == OAUTH_EXCEPTION_CODE:
            return PreflightCheck(
                key="app_subscriptions", ok=False, detail="Meta rechazó las credenciales: revisá META_APP_SECRET"
            )
        if code in RATE_LIMIT_CODES:
            return PreflightCheck(
                key="app_subscriptions", ok=None, detail=f"Rate limit de Meta (code {code}): reintentá más tarde"
            )
        return PreflightCheck(
            key="app_subscriptions", ok=False, detail=f"Error de Meta: {message or response.status_code}"
        )

    items = response.json().get("data", [])
    if not items:
        return PreflightCheck(key="app_subscriptions", ok=False, detail="La app no tiene ninguna suscripción de webhook")

    resumen = "; ".join(
        f"{obj.get('object', '?')}: callback={obj.get('callback_url', '?')} "
        f"active={obj.get('active', False)} fields={len(obj.get('fields', []))}"
        for obj in items
    )
    return PreflightCheck(key="app_subscriptions", ok=True, detail=resumen[:400])


def _check_config_id(config: MetaAppConfig) -> PreflightCheck:
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

    Envuelve todo en `tenant_scope(tenant_id)`: `effective_meta_app_config` ya
    toma `tenant_id` explícito, pero `_check_webhook_handshake` resuelve el
    `verify_token` vía `effective_credentials()`, que depende del contextvar
    *ambiente* (no de un parámetro). Sin este `tenant_scope`, un caller futuro
    que invoque `run_preflight(tenant_id_de_B, ...)` desde un contexto cuyo
    tenant ambiente siga siendo A probaría el webhook de B con credenciales de
    A -- hallazgo del auditor-multitenant, 2026-09-19."""
    async with tenant_scope(tenant_id):
        config = await effective_meta_app_config(tenant_id)
        version = get_settings().graph_api_version

        if config is None:
            return [
                PreflightCheck(key="app_token", ok=False, detail="Meta App no configurada para este tenant"),
                PreflightCheck(key="app_info", ok=False, detail="Meta App no configurada para este tenant"),
                PreflightCheck(key="app_subscriptions", ok=False, detail="Meta App no configurada para este tenant"),
                PreflightCheck(key="config_id", ok=False, detail="META_CONFIG_ID vacío o con formato inválido"),
                await _run_webhook_check_only(tenant_slug),
            ]

        checks: list[PreflightCheck] = []
        async with httpx.AsyncClient(timeout=15) as client:
            token, token_check = await _fetch_app_token(client, config, version)
            checks.append(token_check)

            if token is not None:
                checks.append(await _check_app_info(client, config, version, token))
                checks.append(await _check_subscriptions(client, config, version, token))
            else:
                skip_detail = "Sin token de app válido, no se pudo verificar"
                checks.append(PreflightCheck(key="app_info", ok=token_check.ok, detail=skip_detail))
                checks.append(PreflightCheck(key="app_subscriptions", ok=token_check.ok, detail=skip_detail))

            checks.append(_check_config_id(config))
            checks.append(await _check_webhook_handshake(client, tenant_slug))
        return checks


async def _run_webhook_check_only(tenant_slug: str) -> PreflightCheck:
    async with httpx.AsyncClient(timeout=15) as client:
        return await _check_webhook_handshake(client, tenant_slug)

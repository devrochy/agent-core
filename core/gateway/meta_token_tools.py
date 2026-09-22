"""Herramientas de token para el wizard BYOK: inspeccionar y extender.

- `inspect_token`: `GET /debug_token` -- validez, tipo, expiración (`expires_at`,
  0 = no expira), scopes. Permite confirmar que un token permanente (System
  User) realmente no expira y que trae los permisos de WhatsApp.
- `extend_token`: `GET /oauth/access_token?grant_type=fb_exchange_token` --
  canjea un token temporal (1-24h) por uno long-lived (~60 días).

Meta NO expone API para *generar* un System User token (se crea en Business
Settings); eso se guía en el panel. Acá solo se verifica y se extiende.

Nunca se loguea el token (ni el recibido ni el extendido).
"""

from __future__ import annotations

from typing import Any

import httpx

from core.config import get_settings
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import GRAPH_BASE, graph_fetch
from core.shared.logger import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 30.0

# Mismos nombres tolerados que `meta_onboarding._verify_token_scopes` (Meta a
# veces expone `whatsapp_business_manage_events` en vez de
# `whatsapp_business_management` según la app/momento).
_WHATSAPP_SCOPES = {
    "whatsapp_business_messaging",
    "whatsapp_business_management",
    "whatsapp_business_manage_events",
}


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def inspect_token(
    app_id: str, app_secret: str, token: str, *, version: str | None = None
) -> dict[str, Any]:
    """Inspecciona un token contra `/debug_token`. Parseo tolerante (nunca
    levanta por un campo faltante); sí levanta `GraphError` si Meta rechaza la
    llamada (app_id/app_secret inválidos)."""
    version = version or get_settings().graph_api_version
    app_token = f"{app_id}|{app_secret}"
    data = await graph_fetch(
        "/debug_token",
        token=app_token,
        app_secret=app_secret,
        query={"input_token": token},
        version=version,
    )
    payload = (data or {}).get("data", {}) or {}
    expires_at = payload.get("expires_at")
    scopes = payload.get("scopes") or []
    has_wa = bool(set(scopes) & _WHATSAPP_SCOPES)
    return {
        "is_valid": bool(payload.get("is_valid")),
        "app_id": payload.get("app_id"),
        "type": payload.get("type"),
        # `expires_at` es unixtime; 0 = "no expira" (convención de Meta).
        "expires_at": expires_at,
        "never_expires": expires_at == 0,
        "scopes": scopes,
        "has_whatsapp_scopes": has_wa,
        "user_id": payload.get("user_id"),
    }


async def extend_token(
    app_id: str, app_secret: str, token: str, *, version: str | None = None
) -> dict[str, Any]:
    """Canjea `token` (temporal) por uno long-lived (~60 días).

    Usa `oauth/access_token` directo (endpoint público: NO lleva
    `appsecret_proof`). Devuelve `{access_token, expires_in}`. Levanta
    `GraphError` clasificado si Meta rechaza."""
    version = version or get_settings().graph_api_version
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": token,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{GRAPH_BASE}/{version}/oauth/access_token", params=params)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise GraphError(f"No se pudo contactar a Meta para extender el token: {exc}", category="TRANSIENT") from exc

    if response.status_code >= 400:
        raise classify_and_build_error(
            http_status=response.status_code,
            body=_safe_json(response),
            headers=response.headers,
            raw_text=response.text,
        )

    body = _safe_json(response)
    new_token = body.get("access_token")
    if not new_token:
        raise GraphError("Meta no devolvió access_token al extender", category="TRANSIENT")
    return {"access_token": new_token, "expires_in": body.get("expires_in")}

"""Flujo OAuth2 de Google (authorization code + refresh token).

Los tokens se guardan CIFRADOS en la tabla plugin_configs bajo la clave
"_oauth_tokens" (el sufijo _ los excluye del formulario del panel).

Flujo:
1. Admin abre /admin/plugins/pack_calendar/oauth -> redirect a Google.
2. Google redirige a /admin/plugins/pack_calendar/oauth/callback?code=...
3. Se intercambia el code por access_token + refresh_token y se persisten.
4. get_access_token() refresca automáticamente cuando expira.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from core.config import get_settings
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger

log = get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/calendar.events"
PLUGIN_ID = "pack_calendar"


def redirect_uri() -> str:
    return f"{get_settings().public_base_url}/admin/plugins/{PLUGIN_ID}/oauth/callback"


def build_authorization_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # imprescindible para recibir refresh_token
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(client_id: str, client_secret: str, code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri(),
            },
        )
    if response.status_code != 200:
        raise PluginError(PLUGIN_ID, f"OAuth exchange falló: {response.text[:200]}")
    tokens: dict[str, Any] = response.json()
    tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 3600)) - 60
    return tokens


async def save_tokens(tokens: dict[str, Any]) -> None:
    """Persiste los tokens dentro de la config cifrada del plugin."""
    from core.db.models import PluginConfig
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import get_current_tenant_id

    box = get_secret_box()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        stored = (
            await session.execute(
                select(PluginConfig).where(
                    PluginConfig.plugin_id == PLUGIN_ID,
                    PluginConfig.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        values = box.decrypt_json(stored.config_encrypted) if stored else {}
        # conservar el refresh_token previo si Google no devuelve uno nuevo
        previous = values.get("_oauth_tokens", {})
        if "refresh_token" not in tokens and "refresh_token" in previous:
            tokens["refresh_token"] = previous["refresh_token"]
        values["_oauth_tokens"] = tokens
        encrypted = box.encrypt_json(values)
        if stored is None:
            session.add(
                PluginConfig(plugin_id=PLUGIN_ID, tenant_id=tenant_id, config_encrypted=encrypted)
            )
        else:
            stored.config_encrypted = encrypted


async def get_access_token(client_id: str, client_secret: str) -> str:
    """Devuelve un access_token válido, refrescando si expiró."""
    from core.db.models import PluginConfig
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import get_current_tenant_id

    box = get_secret_box()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        stored = (
            await session.execute(
                select(PluginConfig).where(
                    PluginConfig.plugin_id == PLUGIN_ID,
                    PluginConfig.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
    if stored is None:
        raise PluginError(PLUGIN_ID, "Plugin sin configurar")
    values = box.decrypt_json(stored.config_encrypted)
    tokens = values.get("_oauth_tokens")
    if not tokens:
        raise PluginError(
            PLUGIN_ID,
            "Cuenta de Google no conectada. Abre /admin/plugins/pack_calendar/oauth",
        )

    if time.time() < float(tokens.get("expires_at", 0)):
        return str(tokens["access_token"])

    # refresh
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise PluginError(PLUGIN_ID, "Sin refresh_token: reconecta la cuenta de Google")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code != 200:
        raise PluginError(PLUGIN_ID, f"Refresh de token falló: {response.text[:200]}")
    new_tokens: dict[str, Any] = response.json()
    new_tokens["expires_at"] = time.time() + float(new_tokens.get("expires_in", 3600)) - 60
    new_tokens.setdefault("refresh_token", refresh_token)
    await save_tokens(new_tokens)
    return str(new_tokens["access_token"])

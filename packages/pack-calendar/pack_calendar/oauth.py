"""Flujo OAuth2 de Google (authorization code + refresh token).

Los tokens viven en la **conexión** del tenant (`channel_connections`, tipo
`google_calendar`, credenciales cifradas con SecretBox) — ADR-0069/0070. Durante
una ventana de compatibilidad se sigue escribiendo/leyendo también la copia vieja
en `plugin_configs` (clave `_oauth_tokens`), sin backfill: cada tenant reconcilia
la próxima vez que se guarda.

Flujo:
1. Admin abre /admin/plugins/pack_calendar/oauth -> redirect a Google.
2. Google redirige a /admin/plugins/pack_calendar/oauth/callback?code=...
3. Se intercambia el code por access_token + refresh_token y se persisten
   (conexión + copia en plugin_configs).
4. get_access_token() lee de la conexión (con fallback a plugin_configs) y
   refresca automáticamente cuando expira.
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
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
# calendar.events: agendar. userinfo.email: leer el email de la cuenta
# (external_id de la conexión; el exchange del `code` no lo devuelve).
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/userinfo.email"
)
PLUGIN_ID = "pack_calendar"
CONNECTION_TYPE = "google_calendar"


def redirect_uri() -> str:
    return f"{get_settings().public_base_url}/admin/plugins/{PLUGIN_ID}/oauth/callback"


def api_redirect_uri() -> str:
    """Callback servido por la API (`/api/v1`), que es la superficie vigente."""
    return f"{get_settings().public_base_url}/api/v1/pack/{PLUGIN_ID}/oauth/callback"


def build_authorization_url(client_id: str, *, redirect: str | None = None) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect or redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # imprescindible para recibir refresh_token
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect: str | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect or redirect_uri(),
            },
        )
    if response.status_code != 200:
        raise PluginError(PLUGIN_ID, f"OAuth exchange falló: {response.text[:200]}")
    tokens: dict[str, Any] = response.json()
    tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 3600)) - 60
    return tokens


async def _account_email(access_token: str) -> str:
    """Email de la cuenta autorizada, para el `external_id` de la conexión.

    El exchange del `code` no lo devuelve; hay que pedirlo a userinfo. Si falla,
    la conexión igual se guarda (con email vacío) y se loguea.
    """
    if not access_token:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
        if response.status_code == 200:
            return str(response.json().get("email", "") or "")
        log.warning("google_userinfo_status", status=response.status_code)
    except Exception as exc:
        log.warning("google_userinfo_failed", error=type(exc).__name__)
    return ""


async def _connection_tokens() -> dict[str, Any] | None:
    """Tokens desde la conexión del tenant (fuente nueva, ADR-0070)."""
    from core.gateway.connections import resolve_connection_credentials

    creds = await resolve_connection_credentials(CONNECTION_TYPE)
    if creds and creds.get("access_token"):
        return creds
    return None


async def _plugin_config_tokens() -> dict[str, Any] | None:
    """Copia vieja en `plugin_configs` (ventana de compatibilidad)."""
    from core.db.models import PluginConfig
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import get_current_tenant_id

    box = get_secret_box()
    async with get_session() as session:
        stored = (
            await session.execute(
                select(PluginConfig).where(
                    PluginConfig.plugin_id == PLUGIN_ID,
                    PluginConfig.tenant_id == get_current_tenant_id(),
                    PluginConfig.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if stored is None:
        return None
    values = box.decrypt_json(stored.config_encrypted)
    return values.get("_oauth_tokens") or None


async def save_tokens(tokens: dict[str, Any], *, from_refresh: bool = False) -> None:
    """Persiste los tokens en la conexión del tenant + copia en `plugin_configs`.

    `from_refresh=True` (refresco de la misma cuenta) permite conservar el email
    previo si userinfo falla. En un alta nueva, sin email no se guarda: evita
    pisar la conexión de la cuenta anterior con el `external_id` equivocado.
    """
    existing = await _connection_tokens()
    previous = existing or await _plugin_config_tokens() or {}
    # Google no devuelve refresh_token en cada refresh: conservar el previo.
    if not tokens.get("refresh_token") and previous.get("refresh_token"):
        tokens["refresh_token"] = previous["refresh_token"]

    email = await _account_email(str(tokens.get("access_token", "")))
    if not email and from_refresh:
        email = str(previous.get("account_email", ""))
    if not email:
        raise PluginError(
            PLUGIN_ID,
            "No se pudo obtener el email de la cuenta de Google. Reintentá conectar la cuenta.",
        )

    # 1) Conexión del tenant (fuente nueva). `save_connection` normaliza contra el
    #    credentials_schema y conserva el refresh_token previo si viniera vacío.
    from core.gateway.connections import save_connection

    try:
        await save_connection(
            "Google Calendar",
            {
                "access_token": tokens.get("access_token", ""),
                "refresh_token": tokens.get("refresh_token", ""),
                "expires_at": int(float(tokens.get("expires_at", 0))),
                "account_email": email,
            },
            type_id=CONNECTION_TYPE,
        )
    except Exception:
        # No tumbar la conexión si falla el guardado nuevo: la copia vieja
        # (paso 2) mantiene el flujo funcionando durante la ventana.
        log.exception("calendar_connection_save_failed")

    # 2) Dual-write a plugin_configs (ventana de compatibilidad, sin backfill).
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
        values["_oauth_tokens"] = tokens
        encrypted = box.encrypt_json(values)
        if stored is None:
            session.add(
                PluginConfig(plugin_id=PLUGIN_ID, tenant_id=tenant_id, config_encrypted=encrypted)
            )
        else:
            stored.config_encrypted = encrypted


async def get_access_token(client_id: str, client_secret: str) -> str:
    """Devuelve un access_token válido, refrescando si expiró.

    Lee primero la conexión del tenant y cae a la copia en `plugin_configs`
    mientras dure la ventana de compatibilidad.
    """
    tokens = await _connection_tokens() or await _plugin_config_tokens()
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
    if tokens.get("account_email"):
        new_tokens.setdefault("account_email", tokens["account_email"])
    await save_tokens(new_tokens, from_refresh=True)
    return str(new_tokens["access_token"])

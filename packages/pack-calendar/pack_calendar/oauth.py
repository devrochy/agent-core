"""Flujo OAuth2 de Google (authorization code + refresh token).

Los tokens viven en la **conexión** del tenant (`channel_connections`, tipo
`google_calendar`, credenciales cifradas con SecretBox) — ADR-0069/0070. Durante
una ventana de compatibilidad se sigue escribiendo/leyendo también la copia vieja
en `plugin_configs` (clave `_oauth_tokens`), sin backfill: cada tenant reconcilia
la próxima vez que se guarda.

Flujo (HU-CAL-07, ADR-0093):
1. El panel llama a la API `/api/v1/pack/calendar/oauth/start` (autenticado), que
   deja la cookie `oauth_state` y redirige a Google con `state` y el retorno del panel.
2. Google redirige a `<panel>/api/calendar/oauth/callback?code=...&state=...`; la
   ruta BFF del panel llama a la API `/oauth/callback` con la sesión y el negocio.
3. La API valida el `state`, intercambia el code por access_token + refresh_token
   y los persiste (conexión + copia en plugin_configs).
4. get_access_token() lee de la conexión (con fallback a plugin_configs) y
   refresca automáticamente cuando expira.

Varias cuentas (HU-CAL-08, ADR-0170): una conexión de Google = una agenda. Los tokens se
leen y se refrescan **por `connection_id`**; sin él se usa la conexión por defecto del
negocio (compatibilidad). Una cuenta nueva solo hereda el `refresh_token` de la conexión
con su mismo correo, nunca el de otra cuenta. Reconectar una cuenta compara el correo con
el de la conexión y no pisa nada si es otra.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select

from core.config import get_settings
from core.mcp.types import ScopedHttp
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_calendar import google_api

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


def panel_redirect_uri() -> str:
    """Retorno de Google: la ruta BFF del panel (D-SPEC-24-11, ADR-0093).

    En los entornos con túnel, todo `/api/v1/*` lo atiende el panel; el retorno
    entra por el panel, que trae la sesión y el negocio del usuario y llama a la API.
    Es la URI que cada negocio registra en su Google Cloud Console.
    """
    settings = get_settings()
    base = (settings.panel_base_url or settings.public_base_url).rstrip("/")
    return f"{base}/api/calendar/oauth/callback"


def build_authorization_url(
    client_id: str, *, redirect: str | None = None, state: str = "", login_hint: str = ""
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect or redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # imprescindible para recibir refresh_token
        "prompt": "consent",
    }
    if state:
        params["state"] = state  # anti-CSRF (D-SPEC-24-12)
    if login_hint:
        params["login_hint"] = login_hint  # reconectar: sugiere la misma cuenta
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect: str | None = None,
) -> dict[str, Any]:
    response = await google_api.request(
        google_api.default_http(),
        "POST",
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect or redirect_uri(),
        },
    )
    if response["status"] != 200:
        raise PluginError(PLUGIN_ID, f"OAuth exchange falló: {google_api.snippet(response['data'])}")
    tokens: dict[str, Any] = response["data"]
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
        response = await google_api.request(
            google_api.default_http(), "GET", USERINFO_URL, token=access_token, timeout=15
        )
        if response["status"] == 200 and isinstance(response["data"], dict):
            return str(response["data"].get("email", "") or "")
        log.warning("google_userinfo_status", status=response["status"])
    except Exception as exc:
        log.warning("google_userinfo_failed", error=type(exc).__name__)
    return ""


class OtraCuentaError(PluginError):
    """Se reconectó con una cuenta de Google distinta a la de la conexión (no se guarda nada)."""


async def _connection_tokens(connection_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    """Tokens desde la conexión del tenant (fuente nueva, ADR-0070).

    Con `connection_id` lee esa conexión (de este negocio y de tipo google_calendar); sin él,
    la conexión activa por defecto del tipo (compatibilidad con quien no conoce la agenda).
    """
    from core.gateway.connections import (
        credentials_of,
        get_connection,
        resolve_connection_credentials,
    )

    if connection_id is not None:
        connection = await get_connection(connection_id)
        if (
            connection is None
            or connection.deleted_at is not None
            or connection.type_id != CONNECTION_TYPE
            or connection.tenant_id != get_current_tenant_id()
        ):
            return None
        creds = credentials_of(connection)
    else:
        creds = await resolve_connection_credentials(CONNECTION_TYPE)
    if creds and creds.get("access_token"):
        return creds
    return None


async def conexion_por_email(email: str) -> Any | None:
    """Conexión de Google del negocio con ese correo (la activa primero), o None."""
    from core.db.models import ChannelConnection
    from core.db.session import get_session

    if not email:
        return None
    async with get_session() as session:
        return (
            await session.execute(
                select(ChannelConnection)
                .where(
                    ChannelConnection.tenant_id == get_current_tenant_id(),
                    ChannelConnection.type_id == CONNECTION_TYPE,
                    ChannelConnection.external_id == email,
                )
                .order_by(ChannelConnection.deleted_at.is_not(None), ChannelConnection.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


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


async def save_tokens(
    tokens: dict[str, Any],
    *,
    from_refresh: bool = False,
    connection_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Persiste los tokens en la conexión del tenant y devuelve su id.

    - `from_refresh=True`: refresco de la MISMA conexión (`connection_id`); permite conservar el
      email previo si userinfo falla.
    - `connection_id` sin `from_refresh`: reconectar esa conexión. Si la cuenta autorizada es
      otra (otro correo), lanza `OtraCuentaError` y no guarda nada.
    - Sin `connection_id`: alta. Con el correo de la cuenta se busca SU conexión; solo de esa se
      hereda el `refresh_token`. Sin correo no se guarda (evita pisar otra cuenta con un
      `external_id` equivocado).

    La copia vieja en `plugin_configs` (ventana de compatibilidad) solo la escribe la conexión
    de la agenda principal; las cuentas adicionales no la tocan.
    """
    email = await _account_email(str(tokens.get("access_token", "")))
    objetivo = connection_id
    previous: dict[str, Any] = {}
    if objetivo is not None:
        previous = await _connection_tokens(objetivo) or {}
        if not previous:
            raise PluginError(PLUGIN_ID, "La conexión de Google no existe o no es de este negocio.")
        if not email and from_refresh:
            email = str(previous.get("account_email", ""))
        anterior = str(previous.get("account_email", "") or "")
        if email and anterior and email.lower() != anterior.lower():
            raise OtraCuentaError(
                PLUGIN_ID, "Autorizaste otra cuenta de Google distinta a la de esta conexión."
            )
    else:
        if email:
            hallada = await conexion_por_email(email)
            if hallada is not None and hallada.deleted_at is None:
                from core.gateway.connections import credentials_of

                previous = credentials_of(hallada)
                objetivo = hallada.id
        elif from_refresh:
            # Quien refresca sin conocer la conexión (compatibilidad): la cuenta por defecto.
            previous = await _connection_tokens() or await _plugin_config_tokens() or {}
            email = str(previous.get("account_email", ""))
    # Google no devuelve refresh_token en cada refresh: conservar el de ESTA cuenta.
    if not tokens.get("refresh_token") and previous.get("refresh_token"):
        tokens["refresh_token"] = previous["refresh_token"]
    if not email:
        raise PluginError(
            PLUGIN_ID,
            "No se pudo obtener el email de la cuenta de Google. Reintentá conectar la cuenta.",
        )

    # 1) Conexión del tenant (fuente nueva). `save_connection` normaliza contra el
    #    credentials_schema y conserva el refresh_token previo si viniera vacío.
    from core.gateway.connections import save_connection

    guardada_id: uuid.UUID | None = objetivo
    try:
        conexion, _ = await save_connection(
            "Google Calendar" if objetivo is None else (await _etiqueta(objetivo)) or "Google Calendar",
            {
                "access_token": tokens.get("access_token", ""),
                "refresh_token": tokens.get("refresh_token", ""),
                "expires_at": int(float(tokens.get("expires_at", 0))),
                "account_email": email,
            },
            connection_id=objetivo,
            type_id=CONNECTION_TYPE,
        )
        guardada_id = conexion.id
    except Exception:
        # No tumbar la conexión si falla el guardado nuevo: la copia vieja
        # (paso 2) mantiene el flujo funcionando durante la ventana.
        log.exception("calendar_connection_save_failed")

    # 2) Dual-write a plugin_configs (ventana de compatibilidad, sin backfill): solo para la
    #    conexión de la agenda principal.
    if await _es_conexion_principal(guardada_id):
        await _escribir_copia_vieja(tokens)
    return guardada_id


async def _etiqueta(connection_id: uuid.UUID) -> str:
    from core.gateway.connections import get_connection

    connection = await get_connection(connection_id)
    return (connection.label if connection is not None else "") or ""


async def _es_conexion_principal(connection_id: uuid.UUID | None) -> bool:
    """¿Es la conexión de la agenda principal (o aún no hay agenda vinculada)?"""
    from pack_calendar import agendas

    try:
        principal = next((a for a in await agendas.listar() if a.is_default), None)
    except Exception:
        return True  # sin poder leer las agendas, comportamiento de siempre
    return principal is None or principal.connection_id in (None, connection_id)


async def _escribir_copia_vieja(tokens: dict[str, Any]) -> None:
    from core.db.models import PluginConfig
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box

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


async def get_access_token(
    client_id: str,
    client_secret: str,
    *,
    http: ScopedHttp | None = None,
    connection_id: uuid.UUID | None = None,
) -> str:
    """Devuelve un access_token válido, refrescando si expiró.

    Lee primero la conexión del tenant y cae a la copia en `plugin_configs`
    mientras dure la ventana de compatibilidad. El refresh sale por `http` (el
    `context.http` de la tool) o, sin él, por el cliente acotado del pack.
    """
    tokens = await _connection_tokens(connection_id)
    if not tokens and connection_id is None:
        tokens = await _plugin_config_tokens()
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
    response = await google_api.request(
        http or google_api.default_http(),
        "POST",
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if response["status"] != 200:
        raise PluginError(PLUGIN_ID, f"Refresh de token falló: {google_api.snippet(response['data'])}")
    new_tokens: dict[str, Any] = response["data"]
    new_tokens["expires_at"] = time.time() + float(new_tokens.get("expires_in", 3600)) - 60
    new_tokens.setdefault("refresh_token", refresh_token)
    if tokens.get("account_email"):
        new_tokens.setdefault("account_email", tokens["account_email"])
    await save_tokens(new_tokens, from_refresh=True, connection_id=connection_id)
    return str(new_tokens["access_token"])

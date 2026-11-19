"""Instagram Login (ruta b de HU-CHAN-04, ADR-0152): la cuenta profesional se autoriza directamente con Instagram.

Es OTRA app de Meta (la «app de Instagram» del producto): su propio App ID/Secret (`INSTAGRAM_APP_ID/SECRET`), sus
propios permisos (`instagram_business_basic`, `instagram_business_manage_messages`) y sus propios hosts
(`instagram.com`, `api.instagram.com`, `graph.instagram.com`). Nada de aquí pasa por `meta_graph.graph_fetch` (que
fija `graph.facebook.com`) y NUNCA se mezcla con los permisos de Facebook Login en el mismo diálogo.

Flujo: `authorize` (el navegador) → `code` de una hora y un solo uso → token corto (`api.instagram.com`, formulario) →
token largo de 60 días (`graph.instagram.com`, `ig_exchange_token`) → `GET /me`. El token largo se REFRESCA (no se
canjea de nuevo) con `ig_refresh_token`: exige que tenga al menos 24 h y no haya vencido
(`core/tasks/instagram_token_refresh.py`).

Los datos de los endpoints salen de los resultados de búsqueda de Meta y del código viejo (el entorno de desarrollo no
llega a la documentación): la ruta está detrás de `INSTAGRAM_LOGIN_ENABLED` y se prende solo tras probarla con una
cuenta real en test (ADR-0152, adenda de la entrega 4b).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from core.config import get_settings
from core.db.models import utcnow
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

IG_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH_BASE = "https://graph.instagram.com"
IG_SCOPES = "instagram_business_basic,instagram_business_manage_messages"
IG_LONG_LIVED_TOKEN_KIND = "long_lived_user"
LONG_LIVED_DAYS = 60
REFRESH_MIN_AGE_HOURS = 24
REFRESH_WINDOW_DAYS = 10
# Campos de mensajería que se piden al suscribir la cuenta (el parámetro es `subscribed_fields`).
IG_LOGIN_SUBSCRIBED_FIELDS = ("messages", "messaging_postbacks", "messaging_seen", "message_reactions", "messaging_referral")
CAMPO_OBLIGATORIO = "messages"
_TIMEOUT_S = 15.0


class InstagramLoginError(AgentCoreError):
    """Instagram Login no puede seguir (configuración, respuesta sin lo esperado)."""


def faltan_para_instagram_login(settings: Any) -> list[str]:
    """Variables que faltan para la ruta b (vacío si está lista). La bandera no cuenta: es la decisión de prenderla."""
    return [
        nombre
        for nombre, valor in (
            ("INSTAGRAM_APP_ID", settings.instagram_app_id),
            ("INSTAGRAM_APP_SECRET", settings.instagram_app_secret),
            ("INSTAGRAM_LOGIN_REDIRECT_URI", settings.instagram_login_redirect_uri),
        )
        if not valor
    ]


def url_de_autorizacion(state: str) -> str:
    """Dirección a la que el panel manda el navegador. `state` es el intento de un solo uso del alta (atado al negocio
    y al admin). `enable_fb_login=0` fuerza el inicio de sesión de Instagram, no el de Facebook."""
    settings = get_settings()
    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": settings.instagram_login_redirect_uri,
        "response_type": "code",
        "scope": IG_SCOPES,
        "state": state,
        "enable_fb_login": "0",
    }
    return f"{IG_AUTHORIZE_URL}?{urlencode(params)}"


def _json(response: httpx.Response) -> dict[str, Any]:
    """El cuerpo como objeto; una respuesta que no es JSON (un proxy, una página de error) es un error de Instagram, no
    una excepción suelta."""
    try:
        cuerpo = response.json()
    except ValueError as exc:
        raise InstagramLoginError("Instagram devolvió una respuesta que no se puede leer.") from exc
    return cuerpo if isinstance(cuerpo, dict) else {}


def _vencimiento(cuerpo: dict[str, Any]) -> datetime:
    """Cuándo vence el token: `expires_in` de Meta (segundos) o, si falta o no es un número, 60 días."""
    try:
        segundos = int(cuerpo.get("expires_in") or 0)
    except (TypeError, ValueError):
        segundos = 0
    return utcnow() + (timedelta(seconds=segundos) if segundos > 0 else timedelta(days=LONG_LIVED_DAYS))


def _error(response: httpx.Response) -> GraphError:
    try:
        body = response.json()
    except Exception:
        body = None
    return classify_and_build_error(
        http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text
    )


async def canjear_codigo(code: str) -> tuple[str, str | None]:
    """`POST api.instagram.com/oauth/access_token` (formulario, no JSON): token corto y, si Meta lo da, el `user_id` de
    la cuenta profesional. El `code` vale una hora y es de un solo uso."""
    settings = get_settings()
    data = {
        "client_id": settings.instagram_app_id,
        "client_secret": settings.instagram_app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": settings.instagram_login_redirect_uri,
        "code": code,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(IG_TOKEN_URL, data=data)
    del data
    if response.status_code >= 400:
        raise _error(response)
    cuerpo = _json(response)
    # La respuesta puede venir envuelta en `data: [{...}]` en versiones viejas del producto.
    if isinstance(cuerpo.get("data"), list) and cuerpo["data"]:
        cuerpo = cuerpo["data"][0]
    token = cuerpo.get("access_token")
    if not token:
        raise InstagramLoginError("Instagram no devolvió el token en el canje del código.")
    user_id = cuerpo.get("user_id")
    return str(token), (str(user_id) if user_id else None)


async def canjear_por_token_largo(token_corto: str) -> tuple[str, datetime]:
    """`GET graph.instagram.com/access_token?grant_type=ig_exchange_token`: token de 60 días."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.get(
            f"{IG_GRAPH_BASE}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.instagram_app_secret,
                "access_token": token_corto,
            },
        )
    if response.status_code >= 400:
        raise _error(response)
    cuerpo = _json(response)
    token = cuerpo.get("access_token")
    if not token:
        raise InstagramLoginError("Instagram no devolvió el token de larga duración.")
    return str(token), _vencimiento(cuerpo)


async def refrescar_token(token: str) -> tuple[str, datetime]:
    """`GET graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token`: otros 60 días. Meta lo rechaza si el
    token tiene menos de 24 h, si ya venció o si no tiene `instagram_business_basic`."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.get(
            f"{IG_GRAPH_BASE}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )
    if response.status_code >= 400:
        raise _error(response)
    cuerpo = _json(response)
    nuevo = cuerpo.get("access_token")
    if not nuevo:
        raise GraphError("Instagram no devolvió el token en el refresco", category="PERMANENT")
    return str(nuevo), _vencimiento(cuerpo)


async def datos_de_la_cuenta(token: str) -> tuple[str, str | None]:
    """`GET graph.instagram.com/me?fields=user_id,username`. `user_id` es la cuenta profesional (el `entry.id` del
    webhook); `id` es el identificador de la app y solo se usa si Meta no manda `user_id`."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.get(
            f"{IG_GRAPH_BASE}/me", params={"fields": "user_id,username"}, headers={"Authorization": f"Bearer {token}"}
        )
    if response.status_code >= 400:
        raise _error(response)
    cuerpo = _json(response)
    ig_user_id = cuerpo.get("user_id") or cuerpo.get("id")
    if not ig_user_id:
        raise InstagramLoginError("Instagram no devolvió el identificador de la cuenta.")
    return str(ig_user_id), cuerpo.get("username")


async def suscribir_y_verificar(ig_user_id: str, token: str, version: str) -> tuple[bool, tuple[str, ...]]:
    """`POST /{ig_user_id}/subscribed_apps` y, enseguida, un GET: se guardan los campos que Meta confirma. Solo cuenta
    como suscrita si confirma `messages`."""
    base = f"{IG_GRAPH_BASE}/{version}/{ig_user_id}/subscribed_apps"
    cabecera = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        respuesta = await client.post(
            base, params={"subscribed_fields": ",".join(IG_LOGIN_SUBSCRIBED_FIELDS)}, headers=cabecera
        )
        if respuesta.status_code >= 400:
            raise _error(respuesta)
        verificacion = await client.get(base, headers=cabecera)
    if verificacion.status_code >= 400:
        raise _error(verificacion)
    campos: set[str] = set()
    for entrada in _json(verificacion).get("data", []):
        campos.update(str(c) for c in entrada.get("subscribed_fields", []))
    return CAMPO_OBLIGATORIO in campos, tuple(sorted(campos))


async def enviar_mensaje(ig_user_id: str, token: str, version: str, payload: dict[str, object]) -> str:
    """`POST graph.instagram.com/{version}/{ig_user_id}/messages` con el token de la cuenta."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{IG_GRAPH_BASE}/{version}/{ig_user_id}/messages",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise _error(response)
    return str(_json(response).get("message_id", ""))

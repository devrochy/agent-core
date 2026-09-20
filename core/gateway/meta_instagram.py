"""Conexión de Instagram: dos rutas de login soportadas en paralelo.

- Ruta (a) "Facebook Login": la cuenta de IG está vinculada a una Página de
  Facebook. Mismo host (`graph.facebook.com`), mismo `graph_fetch`, mismo
  App ID/Secret del framework (`settings.meta_app_id`) que Messenger/
  WhatsApp -- pero con una Login Configuration DEDICADA
  (`settings.meta_config_id_instagram`) para no mezclar el juego de
  permisos de Instagram con el de Messenger en el mismo diálogo. El envío
  reusa el token de Página ya construido para Messenger
  (`core/gateway/meta_messenger.py`), no se guarda un token nuevo.
- Ruta (b) "Instagram Login": app de Meta DISTINTA (`settings.
  instagram_app_id`/`instagram_app_secret`, numérica), hosts
  `instagram.com`/`api.instagram.com`/`graph.instagram.com` -- NO pasa por
  `graph_fetch` (que hardcodea `graph.facebook.com`), llamadas `httpx`
  directas, mismo estilo que `meta_onboarding.py::fetch_exchange_token`.

NUNCA se piden los dos juegos de permisos en el mismo diálogo -- son dos
flujos de OAuth completamente separados, cada uno con su propia app/
config_id, y ninguna función de este módulo mezcla settings de una ruta
con la otra.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from core.config import get_settings
from core.db.models import IgAccount, MetaConnection, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import graph_fetch
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

IG_LONG_LIVED_TOKEN_KIND = "long_lived_user"
LONG_LIVED_DAYS = 60
REFRESH_MIN_AGE_HOURS = 24
REFRESH_WINDOW_DAYS = 10
_MANAGE_TASKS = {"MANAGE", "MODERATE"}

INSTAGRAM_SUBSCRIBED_FIELDS = (
    "messages,messaging_postbacks,messaging_seen,message_reactions,messaging_referral,comments,mentions"
)
# Estos dos campos exigen Advanced Access y (comments/mentions) cuenta
# pública -- se detectan reactivamente si Meta no los confirma, nunca se
# adivina cuál de las dos causas es.
_RESTRICTED_FIELDS = {"comments", "mentions"}

# Instagram es más restrictivo que Messenger a propósito: fuera de ventana
# solo HUMAN_AGENT, y solo con mensaje escrito por un humano.
IG_ALLOWED_TAGS = {"HUMAN_AGENT"}

MAX_TEXT_LENGTH = 1000
MAX_IMAGES_PER_REQUEST = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg"}

IG_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_EXCHANGE_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH_BASE = "https://graph.instagram.com"
_TIMEOUT_S = 15.0


class InstagramError(AgentCoreError):
    """Error de esta capa (permiso insuficiente, state inválido, conexión
    no encontrada)."""


class InstagramSendError(AgentCoreError):
    """Envío bloqueado ANTES de llamar a Meta (validación local de medios/
    texto, fuera de ventana sin confirmación, sin conversación previa, o
    ruta (b) con el feature flag apagado)."""


@dataclass
class IgFacebookLoginCandidate:
    page_id: str
    page_name: str
    page_category: str | None
    page_access_token: str | None
    tasks: list[str]
    ig_user_id: str | None
    ig_username: str | None
    insufficient_permission: bool
    reason: str | None = None


# ---------------------------------------------------------------- ruta (a)


async def discover_ig_via_pages(
    user_token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> list[IgFacebookLoginCandidate]:
    """`GET /me/accounts?fields=...,instagram_business_account{id,username}`
    -- una sola llamada descubre la Página Y la cuenta de IG vinculada.
    Mismo filtro de `tasks` que `meta_messenger.discover_pages` (MANAGE/
    MODERATE, si no alcanza no se persiste ni se guarda un token)."""
    data = await graph_fetch(
        "/me/accounts", token=user_token, app_secret=app_secret,
        query={"fields": "id,name,category,access_token,tasks,instagram_business_account{id,username}"},
        version=version, connection_id=connection_id,
    )
    candidates: list[IgFacebookLoginCandidate] = []
    for item in (data or {}).get("data", []):
        ig = item.get("instagram_business_account")
        if not ig:
            continue  # esta Página no tiene una cuenta de IG vinculada
        tasks = item.get("tasks") or []
        insufficient = not (_MANAGE_TASKS & set(tasks))
        candidates.append(
            IgFacebookLoginCandidate(
                page_id=item.get("id", ""), page_name=item.get("name", ""), page_category=item.get("category"),
                page_access_token=None if insufficient else item.get("access_token"), tasks=tasks,
                ig_user_id=ig.get("id"), ig_username=ig.get("username"), insufficient_permission=insufficient,
                reason=(
                    "Falta el rol MANAGE o MODERATE sobre la Página vinculada -- pedile a un admin que te lo "
                    "asigne en Meta Business Suite."
                ) if insufficient else None,
            )
        )
    return candidates


async def connect_ig_facebook_login(
    connection_id: uuid.UUID, tenant_id: uuid.UUID, candidate: IgFacebookLoginCandidate
) -> IgAccount:
    """Persiste la Página (reusa `meta_messenger.connect_page`, mismo token
    de Página) Y la `IgAccount` vinculada -- NO guarda un token nuevo para
    Instagram, el envío de la ruta (a) usa el de la `FbPage`."""
    from core.gateway.meta_messenger import PageCandidate, connect_page

    if candidate.insufficient_permission or not candidate.ig_user_id:
        raise InstagramError(candidate.reason or "Página sin cuenta de Instagram vinculada o sin permiso suficiente")

    page_candidate = PageCandidate(
        page_id=candidate.page_id, name=candidate.page_name, category=candidate.page_category,
        tasks=candidate.tasks, access_token=candidate.page_access_token, insufficient_permission=False,
    )
    await connect_page(connection_id, tenant_id, page_candidate)

    async with get_session() as session:
        existing = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.ig_user_id == candidate.ig_user_id, IgAccount.connection_id == connection_id,
                    IgAccount.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = IgAccount(
                tenant_id=tenant_id, connection_id=connection_id, ig_user_id=candidate.ig_user_id,
                username=candidate.ig_username, linked_page_id=candidate.page_id, login_route="ig_facebook_login",
            )
            session.add(existing)
        else:
            existing.username = candidate.ig_username
            existing.linked_page_id = candidate.page_id
            existing.login_route = "ig_facebook_login"
        await session.flush()
        return existing


# ---------------------------------------------------------------- ruta (b)


def _state_secret() -> str:
    """SIN fallback a `app_secret_key`: si `INSTAGRAM_APP_SECRET` no está
    configurado, el flujo se corta acá en vez de firmar con un secreto
    compartido/débil (hallazgo auditor-multitenant, 2026-09-24, severidad
    alta condicional a config -- un `state` firmado con el default público
    `app_secret_key="change-me"` es forjable por cualquiera que conozca ese
    default, permitiendo vincular una cuenta de Instagram a un tenant
    ajeno). `instagram_ig_login_iniciar` ya no deja arrancar el flujo sin
    este secreto, pero la función tampoco debe degradarse en silencio si de
    algún modo se la llama sin esa guardia."""
    settings = get_settings()
    if not settings.instagram_app_secret:
        raise InstagramError("INSTAGRAM_APP_SECRET no está configurado -- no se puede firmar el state de OAuth")
    return settings.instagram_app_secret


def sign_state(tenant_id: uuid.UUID, admin_id: uuid.UUID | None) -> str:
    """CSRF: `{tenant_id}:{admin_id}:{hmac}` -- se compara con
    `hmac.compare_digest` (tiempo constante), acompañado de una cookie
    httpOnly de corta vida con el mismo valor (doble verificación)."""
    payload = f"{tenant_id}:{admin_id or ''}"
    signature = hmac.new(_state_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_state(state: str, cookie_value: str | None, *, expected_admin_id: uuid.UUID | None = None) -> uuid.UUID:
    """`state` = `"{tenant_id}:{admin_id}:{firma}"` -- ni `tenant_id` ni
    `admin_id` (UUIDs) ni la firma (hex) contienen `:`, así que un `split`
    simple alcanza. Triple verificación: coincide con la cookie httpOnly,
    la firma HMAC es válida (tiempo constante en ambos casos), Y (si se
    pasa `expected_admin_id`) el `admin_id` firmado coincide con el admin
    que está completando el callback -- defensa en profundidad adicional
    (hallazgo auditor-multitenant, 2026-09-24) más allá de la firma sola."""
    if not cookie_value or not hmac.compare_digest(state, cookie_value):
        raise InstagramError("El state de OAuth no coincide con la cookie -- posible CSRF, reiniciá la conexión")

    parts = state.split(":")
    if len(parts) != 3:
        raise InstagramError("state de OAuth mal formado")
    tenant_id_str, admin_id_str, signature = parts
    payload = f"{tenant_id_str}:{admin_id_str}"

    expected_signature = hmac.new(_state_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise InstagramError("Firma de state inválida -- posible CSRF, reiniciá la conexión")

    if expected_admin_id is not None and admin_id_str != str(expected_admin_id):
        raise InstagramError("El state fue emitido para otro admin -- posible CSRF, reiniciá la conexión")

    return uuid.UUID(tenant_id_str)


def build_authorize_url(redirect_uri: str, state: str) -> str:
    settings = get_settings()
    from urllib.parse import urlencode

    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "instagram_business_basic,instagram_business_manage_messages",
        "state": state,
        "enable_fb_login": "false",  # fuerza credenciales de Instagram, no de Facebook
    }
    return f"{IG_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_short_lived_code(code: str, redirect_uri: str) -> str:
    """`POST https://api.instagram.com/oauth/access_token` -- form-encoded,
    NO JSON (endpoint viejo de Meta). El `code` vale 1 hora y es de un solo
    uso."""
    settings = get_settings()
    data = {
        "client_id": settings.instagram_app_id, "client_secret": settings.instagram_app_secret,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(IG_TOKEN_EXCHANGE_URL, data=data)
    if response.status_code >= 400:
        raise _classify_ig_error(response)
    token = (response.json() or {}).get("access_token")
    if not token:
        raise InstagramError("Instagram no devolvió access_token en el canje")
    return str(token)


async def exchange_long_lived_token(short_lived_token: str) -> tuple[str, datetime]:
    """`GET https://graph.instagram.com/access_token?grant_type=
    ig_exchange_token` -> token de 60 días."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.get(
            f"{IG_GRAPH_BASE}/access_token",
            params={
                "grant_type": "ig_exchange_token", "client_secret": settings.instagram_app_secret,
                "access_token": short_lived_token,
            },
        )
    if response.status_code >= 400:
        raise _classify_ig_error(response)
    token = (response.json() or {}).get("access_token")
    if not token:
        raise InstagramError("Instagram no devolvió access_token en el canje de larga duración")
    return str(token), utcnow() + timedelta(days=LONG_LIVED_DAYS)


async def _get_ig_user_id(long_lived_token: str) -> tuple[str, str | None]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.get(f"{IG_GRAPH_BASE}/me", params={"fields": "id,username", "access_token": long_lived_token})
    if response.status_code >= 400:
        raise _classify_ig_error(response)
    data = response.json() or {}
    ig_user_id = data.get("id")
    if not ig_user_id:
        raise InstagramError("Instagram no devolvió el id de la cuenta")
    return str(ig_user_id), data.get("username")


def _classify_ig_error(response: httpx.Response) -> GraphError:
    try:
        body = response.json()
    except Exception:
        body = None
    return classify_and_build_error(http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text)


async def ensure_ig_connection(tenant_id: uuid.UUID, settings: Any) -> MetaConnection:
    async with get_session() as session:
        existing = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id, MetaConnection.channel == "instagram",
                    MetaConnection.environment == "production",
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing
        connection = MetaConnection(
            tenant_id=tenant_id, channel="instagram", environment="production",
            meta_app_id=settings.instagram_app_id, graph_api_version=settings.graph_api_version, status="draft",
        )
        session.add(connection)
        await session.flush()
        return connection


async def connect_instagram_login(
    tenant_id: uuid.UUID, code: str, redirect_uri: str, *, admin_id: uuid.UUID | None = None
) -> IgAccount:
    """Orquesta la ruta (b) completa: canje corto -> canje largo -> `GET /me`
    -> persiste `MetaConnection`+`IgAccount` -> guarda el token de 60 días."""
    settings = get_settings()
    if not settings.instagram_app_id or not settings.instagram_app_secret:
        raise InstagramError("INSTAGRAM_APP_ID/INSTAGRAM_APP_SECRET no están configurados")

    connection = await ensure_ig_connection(tenant_id, settings)

    short_token = await exchange_short_lived_code(code, redirect_uri)
    long_token, expires_at = await exchange_long_lived_token(short_token)
    ig_user_id, username = await _get_ig_user_id(long_token)

    async with get_session() as session:
        existing = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.ig_user_id == ig_user_id, IgAccount.connection_id == connection.id,
                    IgAccount.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = IgAccount(
                tenant_id=tenant_id, connection_id=connection.id, ig_user_id=ig_user_id,
                username=username, login_route="ig_instagram_login",
            )
            session.add(existing)
        else:
            existing.username = username
            existing.login_route = "ig_instagram_login"
            existing.status = "connected"
            existing.status_reason = None
        await session.flush()
        ig_account_id = existing.id

    # Se guarda ANTES de tocar cualquier otra cosa -- reanudable si el
    # navegador se cierra justo después (mismo criterio que el resto de
    # esta serie de tickets).
    await meta_token_store.put_token(
        connection.id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, long_token, expires_at=expires_at, tenant_id=tenant_id
    )

    async with get_session() as session:
        db_connection = await session.get(MetaConnection, connection.id)
        db_connection.status = "connected"
        db_connection.connected_at = utcnow()
        db_connection.connected_by = admin_id
        return await session.get(IgAccount, ig_account_id)


# ---------------------------------------------------------------- suscripción (ambas rutas)


async def subscribe_ig_webhook(
    ig_user_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID, tenant_id: uuid.UUID
) -> tuple[bool, list[str]]:
    """`POST /{ig_user_id}/subscribed_apps?subscribed_fields=...` --
    verificación `GET` obligatoria inmediata, persiste los fields REALES
    confirmados. Si `comments`/`mentions` faltan, se avisa explícito sin
    adivinar la causa exacta (Advanced Access vs. cuenta no pública)."""
    await graph_fetch(
        f"/{ig_user_id}/subscribed_apps", method="POST", token=token, app_secret=app_secret,
        query={"subscribed_fields": INSTAGRAM_SUBSCRIBED_FIELDS}, version=version, connection_id=connection_id,
    )
    verification = await graph_fetch(
        f"/{ig_user_id}/subscribed_apps", token=token, app_secret=app_secret, version=version, connection_id=connection_id,
    )
    entries = (verification or {}).get("data", [])
    real_fields: list[str] = []
    for entry in entries:
        real_fields.extend(entry.get("subscribed_fields", []))
    subscribed = bool(entries)
    missing_restricted = sorted(_RESTRICTED_FIELDS - set(real_fields))

    async with get_session() as session:
        account = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.ig_user_id == ig_user_id, IgAccount.connection_id == connection_id,
                    IgAccount.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if account is not None:
            account.subscribed_fields = sorted(set(real_fields))
    return subscribed, missing_restricted


# ---------------------------------------------------------------- envío


def validate_instagram_message(text: str, media: list[dict[str, Any]] | None = None) -> None:
    """Validación LOCAL, antes de llamar a Meta -- nunca gasta una llamada
    de Graph API en un mensaje que ya sabemos que va a rebotar."""
    if len(text) >= MAX_TEXT_LENGTH:
        raise InstagramSendError(f"El texto supera los {MAX_TEXT_LENGTH} caracteres permitidos por Instagram")

    media = media or []
    images = [m for m in media if m.get("type", "").startswith("image/")]
    if len(images) > MAX_IMAGES_PER_REQUEST:
        raise InstagramSendError(f"Máximo {MAX_IMAGES_PER_REQUEST} imágenes por request")

    for item in media:
        mime = item.get("type", "")
        size = int(item.get("size_bytes", 0) or 0)
        if mime.startswith("image/"):
            if mime not in _ALLOWED_IMAGE_TYPES:
                raise InstagramSendError("Las imágenes deben ser PNG o JPEG")
            if size > MAX_IMAGE_BYTES:
                raise InstagramSendError(f"Imagen supera los {MAX_IMAGE_BYTES // (1024 * 1024)}MB permitidos")
        elif mime.startswith("video/") and size > MAX_VIDEO_BYTES:
            raise InstagramSendError(f"Video supera los {MAX_VIDEO_BYTES // (1024 * 1024)}MB permitidos")
        elif mime.startswith("audio/") and size > MAX_AUDIO_BYTES:
            raise InstagramSendError(f"Audio supera los {MAX_AUDIO_BYTES // (1024 * 1024)}MB permitidos")


def _check_window_and_tag(service_window_expires_at: datetime | None, human_confirmed: bool) -> dict[str, Any]:
    if service_window_expires_at is None:
        raise InstagramSendError(
            "No hay conversación previa con este usuario -- Instagram no permite iniciar contacto "
            "en frío (cold outreach). Esperá a que el cliente escriba primero."
        )
    if service_window_expires_at > utcnow():
        return {"messaging_type": "RESPONSE"}
    if not human_confirmed:
        raise InstagramSendError(
            "La conversación está fuera de la ventana de 24h. Un agente humano debe escribir y "
            "confirmar el mensaje antes de usar el tag HUMAN_AGENT -- los envíos automatizados "
            "fuera de ventana disparan detección de Meta."
        )
    return {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}


async def send_instagram_message_route_a(
    page_id: str, igsid: str, text: str, access_token: str, version: str, connection_id: uuid.UUID,
    *, service_window_expires_at: datetime | None, human_confirmed: bool = False,
) -> str:
    """Ruta (a): `POST /{page_id}/messages` contra `graph.facebook.com`,
    mismo endpoint/token que Messenger (`instagram_manage_messages` +
    Página vinculada)."""
    validate_instagram_message(text)
    extra = _check_window_and_tag(service_window_expires_at, human_confirmed)
    settings = get_settings()
    body = {"recipient": {"id": igsid}, "message": {"text": text}, **extra}
    data = await graph_fetch(
        f"/{page_id}/messages", method="POST", token=access_token, app_secret=settings.meta_app_secret,
        body=body, version=version, connection_id=connection_id,
    )
    return str((data or {}).get("message_id", ""))


async def send_instagram_message_route_b(
    ig_user_id: str, igsid: str, text: str, access_token: str,
    *, service_window_expires_at: datetime | None, human_confirmed: bool = False,
) -> str:
    """Ruta (b): forma NO verificada contra doc primaria de 2026 -- detrás
    de `settings.instagram_direct_send_enabled`. Probarla contra una cuenta
    de staging antes de habilitarla para clientes reales."""
    settings = get_settings()
    if not settings.instagram_direct_send_enabled:
        raise InstagramSendError(
            "El envío directo por Instagram Login no está habilitado -- su forma no está verificada "
            "contra documentación primaria de 2026. Activalo con INSTAGRAM_DIRECT_SEND_ENABLED solo "
            "después de probarlo contra una cuenta de staging."
        )
    validate_instagram_message(text)
    extra = _check_window_and_tag(service_window_expires_at, human_confirmed)
    body = {"recipient": {"id": igsid}, "message": {"text": text}, **extra}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.post(
            f"{IG_GRAPH_BASE}/{ig_user_id}/messages", json=body, headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise _classify_ig_error(response)
    return str((response.json() or {}).get("message_id", ""))

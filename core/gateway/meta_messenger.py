"""Conexión de Facebook Messenger (Páginas): OAuth vía Facebook Login,
descubrimiento de Páginas, suscripción de webhook por Página, envío con
ventana de servicio de 24h y message tags de 2026, y Conversation Routing.

Es la app del framework (`settings.meta_app_id`/`meta_app_secret`/
`meta_config_id`) la que hace el login en nombre de cada tenant -- mismo
criterio que `core/gateway/meta_onboarding.py` (WhatsApp Embedded Signup),
de donde se reusa `fetch_exchange_token` (el canje del `code` es idéntico:
mismo endpoint `/oauth/access_token`, misma incertidumbre documentada sobre
`redirect_uri`).

PREFERENCIA DE TOKEN (no verificado, ver ticket): cuando el `config_id` de
`FB.login` corresponde a una Login Configuration de Facebook Login for
Business con System User, el propio canje del `code` ya devuelve el
Business Integration System User Access Token ("defaults to never expire"
según Meta) -- no hay un paso documentado adicional de "conseguir" ese
token aparte. NO se implementa el patrón viejo de `fb_exchange_token`
("page access token que nunca expira"), que no está documentado para 2026.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.db.models import FbPage, MetaConnection, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_onboarding import fetch_exchange_token
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

PAGE_TOKEN_KIND = "page_access"
_MANAGE_TASKS = {"MANAGE", "MODERATE"}

# Message tags vigentes en 2026 -- CONFIRMED_EVENT_UPDATE, ACCOUNT_UPDATE y
# POST_PURCHASE_UPDATE fueron retirados el 10/feb/2026. No agregar de
# vuelta sin confirmar contra doc primaria vigente.
ALLOWED_MESSAGE_TAGS = {"HUMAN_AGENT", "CUSTOMER_FEEDBACK"}

# Campos que Messenger pide al suscribir una Página -- el parámetro se
# llama `subscribed_fields`, NUNCA `fields` (error común, Meta lo acepta
# silenciosamente con el nombre equivocado y no suscribe nada).
PAGE_SUBSCRIBED_FIELDS = (
    "messages,messaging_postbacks,messaging_optins,message_deliveries,"
    "message_reads,messaging_referrals,message_echoes,messaging_handovers"
)

SERVICE_WINDOW_HOURS = 24
THREAD_CONTROL_MAX_EXTEND_DAYS = 7


class MessengerError(AgentCoreError):
    """Error de esta capa (permiso insuficiente, conexión no encontrada)."""


class MessengerSendError(AgentCoreError):
    """Envío bloqueado ANTES de llamar a Meta (fuera de ventana sin
    confirmación humana, o no somos dueños del hilo con Conversation
    Routing activo)."""


@dataclass
class PageCandidate:
    page_id: str
    name: str
    category: str | None
    tasks: list[str]
    access_token: str | None
    insufficient_permission: bool
    reason: str | None = None


@dataclass
class ConnectMessengerResult:
    connection_id: uuid.UUID
    connected_pages: list[str] = field(default_factory=list)
    skipped_pages: list[PageCandidate] = field(default_factory=list)


async def _ensure_connection(tenant_id: uuid.UUID, settings: Any) -> MetaConnection:
    async with get_session() as session:
        existing = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "messenger",
                    MetaConnection.environment == "production",
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing

        connection = MetaConnection(
            tenant_id=tenant_id, channel="messenger", environment="production",
            meta_app_id=settings.meta_app_id, meta_config_id=settings.meta_config_id,
            graph_api_version=settings.graph_api_version, status="draft",
        )
        session.add(connection)
        await session.flush()
        return connection


async def _get_client_business_id(user_token: str, app_secret: str, version: str, connection_id: uuid.UUID) -> str | None:
    try:
        data = await graph_fetch(
            "/me", token=user_token, app_secret=app_secret,
            query={"fields": "client_business_id"}, version=version, connection_id=connection_id,
        )
    except GraphError as exc:
        log.warning("messenger_client_business_id_fallo", detail=exc.message)
        return None
    return (data or {}).get("client_business_id")


async def discover_pages(user_token: str, app_secret: str, version: str, connection_id: uuid.UUID) -> list[PageCandidate]:
    """`GET /me/accounts`: filtra por `tasks` -- ANALYZE/ADVERTISE no
    alcanzan para mensajería, hace falta MANAGE o MODERATE."""
    data = await graph_fetch(
        "/me/accounts", token=user_token, app_secret=app_secret,
        query={"fields": "id,name,category,access_token,tasks"}, version=version, connection_id=connection_id,
    )
    candidates: list[PageCandidate] = []
    for item in (data or {}).get("data", []):
        tasks = item.get("tasks") or []
        insufficient = not (_MANAGE_TASKS & set(tasks))
        candidates.append(
            PageCandidate(
                page_id=item.get("id", ""), name=item.get("name", ""), category=item.get("category"),
                tasks=tasks, access_token=None if insufficient else item.get("access_token"),
                insufficient_permission=insufficient,
                reason=(
                    "Falta el rol MANAGE o MODERATE sobre esta Página -- pedile a un admin que te lo "
                    "asigne en Meta Business Suite."
                ) if insufficient else None,
            )
        )
    return candidates


async def connect_page(connection_id: uuid.UUID, tenant_id: uuid.UUID, page: PageCandidate) -> FbPage:
    if page.insufficient_permission or not page.access_token:
        raise MessengerError(page.reason or "Página sin permiso suficiente para conectar")

    async with get_session() as session:
        # tenant_id explícito -- defensa en profundidad, mismo criterio que
        # subscribe_page_webhook (hallazgo auditor-multitenant, 2026-09-23).
        existing = (
            await session.execute(
                select(FbPage).where(
                    FbPage.page_id == page.page_id, FbPage.connection_id == connection_id, FbPage.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = FbPage(
                tenant_id=tenant_id, connection_id=connection_id, page_id=page.page_id,
                name=page.name, category=page.category,
            )
            session.add(existing)
        else:
            existing.name = page.name
            existing.category = page.category
        await session.flush()
        page_row_id = existing.id

    await meta_token_store.put_token(connection_id, PAGE_TOKEN_KIND, page.page_id, page.access_token, tenant_id=tenant_id)

    async with get_session() as session:
        return await session.get(FbPage, page_row_id)


async def subscribe_page_webhook(page_id: str, page_access_token: str, version: str, connection_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """`POST /{page_id}/subscribed_apps?subscribed_fields=...` con el PAGE
    access token (NUNCA el de la app). Verificación OBLIGATORIA inmediata
    vía GET -- persiste los `subscribed_fields` REALES que Meta confirma,
    no los que se pidieron (criterio de aceptación del ticket)."""
    settings = get_settings()
    await graph_fetch(
        f"/{page_id}/subscribed_apps", method="POST", token=page_access_token, app_secret=settings.meta_app_secret,
        query={"subscribed_fields": PAGE_SUBSCRIBED_FIELDS}, version=version, connection_id=connection_id,
    )
    verification = await graph_fetch(
        f"/{page_id}/subscribed_apps", token=page_access_token, app_secret=settings.meta_app_secret,
        version=version, connection_id=connection_id,
    )
    entries = (verification or {}).get("data", [])
    real_fields: list[str] = []
    for entry in entries:
        real_fields.extend(entry.get("subscribed_fields", []))
    subscribed = bool(entries)

    async with get_session() as session:
        page_row = (
            await session.execute(
                select(FbPage).where(FbPage.page_id == page_id, FbPage.connection_id == connection_id, FbPage.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if page_row is not None:
            page_row.subscribed_fields = sorted(set(real_fields))
    return subscribed


async def connect_messenger(tenant_id: uuid.UUID, code: str, *, admin_id: uuid.UUID | None = None) -> ConnectMessengerResult:
    """Orquesta el flujo completo: canje del `code`, `client_business_id`,
    `MetaConnection`, descubrimiento y auto-conexión de cada Página con
    permiso suficiente (el ticket no pide un paso de selección manual --
    "filtra y advierte", no "elegí una"). El user access token nunca se
    persiste: solo se usa transitoriamente para descubrir Páginas."""
    settings = get_settings()
    connection = await _ensure_connection(tenant_id, settings)

    params = {"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": code}
    user_token, _used_redirect_uri = await fetch_exchange_token(settings.graph_api_version, params, settings)

    business_id = await _get_client_business_id(user_token, settings.meta_app_secret, settings.graph_api_version, connection.id)
    async with get_session() as session:
        db_connection = await session.get(MetaConnection, connection.id)
        if business_id:
            db_connection.business_id = business_id
        db_connection.status = "pending"

    candidates = await discover_pages(user_token, settings.meta_app_secret, settings.graph_api_version, connection.id)

    result = ConnectMessengerResult(connection_id=connection.id)
    any_connected = False
    for candidate in candidates:
        if candidate.insufficient_permission:
            result.skipped_pages.append(candidate)
            continue
        try:
            page_row = await connect_page(connection.id, tenant_id, candidate)
            await subscribe_page_webhook(page_row.page_id, candidate.access_token, settings.graph_api_version, connection.id, tenant_id)
            result.connected_pages.append(page_row.page_id)
            any_connected = True
        except GraphError as exc:
            log.warning("messenger_connect_page_fallo", page_id=candidate.page_id, detail=exc.message)
            result.skipped_pages.append(
                PageCandidate(
                    page_id=candidate.page_id, name=candidate.name, category=candidate.category,
                    tasks=candidate.tasks, access_token=None, insufficient_permission=False,
                    reason=f"Error de Meta al conectar/suscribir: {exc.message}",
                )
            )

    async with get_session() as session:
        db_connection = await session.get(MetaConnection, connection.id)
        db_connection.status = "connected" if any_connected else "pending"
        db_connection.connected_at = utcnow() if any_connected else db_connection.connected_at
        db_connection.connected_by = admin_id

    return result


# ---------------------------------------------------------------- envío


def _within_service_window(service_window_expires_at: datetime | None) -> bool:
    return service_window_expires_at is not None and service_window_expires_at > utcnow()


async def send_messenger_message(
    page_id: str, psid: str, text: str, access_token: str, version: str, connection_id: uuid.UUID,
    *,
    service_window_expires_at: datetime | None,
    human_confirmed: bool = False,
    conversation_routing_enabled: bool = False,
) -> str:
    """Valida ventana/tag y Conversation Routing ANTES de llamar a Meta --
    un envío bloqueado localmente nunca gasta una llamada de Graph API."""
    if conversation_routing_enabled:
        owner_id = await get_thread_owner(page_id, psid, access_token, version, connection_id)
        settings = get_settings()
        if owner_id and owner_id != settings.meta_app_id:
            raise MessengerSendError(
                "Esta conversación está controlada por otra app (Conversation Routing activo). "
                "Tomá control de la conversación antes de responder."
            )

    if _within_service_window(service_window_expires_at):
        body: dict[str, Any] = {
            "recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": text},
        }
    else:
        if not human_confirmed:
            raise MessengerSendError(
                "La conversación está fuera de la ventana de 24h. Un agente humano debe confirmar "
                "el envío antes de usar el tag HUMAN_AGENT -- los envíos automatizados fuera de "
                "ventana disparan detección de Meta (unsupported message tag)."
            )
        body = {
            "recipient": {"id": psid}, "messaging_type": "MESSAGE_TAG",
            "tag": "HUMAN_AGENT", "message": {"text": text},
        }

    settings = get_settings()
    try:
        data = await graph_fetch(
            f"/{page_id}/messages", method="POST", token=access_token, app_secret=settings.meta_app_secret,
            body=body, version=version, connection_id=connection_id,
        )
    except GraphError as exc:
        # La referencia del Send API no publica la tabla de códigos de
        # error -- nunca se inventa significado para code/error_subcode,
        # solo se traduce lo que Meta explícitamente ya etiquetó (AUTH,
        # rate limit) o manda en error_user_msg.
        log.error(
            "messenger_send_fallo", page_id=page_id, code=exc.code, error_subcode=exc.error_subcode,
            fbtrace_id=exc.fbtrace_id,
        )
        message = (exc.message or "").lower()
        if "role" in message and ("app" in message or "person" in message):
            raise MessengerError(
                "Standard Access: esta app solo puede mensajear a personas con un rol asignado en la "
                "app -- falta completar App Review para enviar a cualquier usuario en producción."
            ) from exc
        raise
    return str((data or {}).get("message_id", ""))


# ---------------------------------------------------------------- thread control


async def get_thread_owner(page_id: str, psid: str, access_token: str, version: str, connection_id: uuid.UUID) -> str | None:
    data = await graph_fetch(
        f"/{page_id}/thread_owner", token=access_token, app_secret=get_settings().meta_app_secret,
        query={"recipient": psid}, version=version, connection_id=connection_id,
    )
    entries = (data or {}).get("data", [])
    if not entries:
        return None
    return entries[0].get("thread_owner", {}).get("app_id")


async def _thread_control_call(
    page_id: str, psid: str, target_app_id: str | None, access_token: str, version: str, connection_id: uuid.UUID, *, edge: str,
    metadata: str | None = None,
) -> None:
    body: dict[str, Any] = {"recipient": {"id": psid}}
    if target_app_id:
        body["target_app_id"] = target_app_id
    if metadata:
        body["metadata"] = metadata
    await graph_fetch(
        f"/{page_id}/{edge}", method="POST", token=access_token, app_secret=get_settings().meta_app_secret,
        body=body, version=version, connection_id=connection_id,
    )


async def pass_thread_control(page_id, psid, target_app_id, access_token, version, connection_id, *, metadata=None):
    await _thread_control_call(page_id, psid, target_app_id, access_token, version, connection_id, edge="pass_thread_control", metadata=metadata)


async def take_thread_control(page_id, psid, access_token, version, connection_id, *, metadata=None):
    await _thread_control_call(page_id, psid, None, access_token, version, connection_id, edge="take_thread_control", metadata=metadata)


async def release_thread_control(page_id, psid, access_token, version, connection_id, *, metadata=None):
    await _thread_control_call(page_id, psid, None, access_token, version, connection_id, edge="release_thread_control", metadata=metadata)


async def request_thread_control(page_id, psid, access_token, version, connection_id, *, metadata=None):
    await _thread_control_call(page_id, psid, None, access_token, version, connection_id, edge="request_thread_control", metadata=metadata)


async def extend_thread_control(page_id, psid, access_token, version, connection_id, *, days: int, metadata=None):
    """Meta permite extender el control del hilo hasta 7 días -- se valida
    localmente antes de llamar, mismo criterio que la ventana de servicio."""
    if days > THREAD_CONTROL_MAX_EXTEND_DAYS:
        raise MessengerError(f"No se puede extender el control del hilo más de {THREAD_CONTROL_MAX_EXTEND_DAYS} días")
    await _thread_control_call(page_id, psid, None, access_token, version, connection_id, edge="extend_thread_control", metadata=metadata)

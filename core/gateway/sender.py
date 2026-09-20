"""Envío de respuestas: resuelve conexión, divide en burbujas y despacha.

- Burbujas con pausa entre envíos (UX natural en WhatsApp).
- Hasta 2 intentos por burbuja.
- La imagen (si hay) va SOLO en la última burbuja.
- Credenciales: channel_connections (cifradas) o fallback a .env.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from core.config import get_settings
from core.db.models import (
    ChannelConnection,
    Conversation,
    Customer,
    FbPage,
    IgAccount,
    Message,
    MetaConnection,
    PhoneNumber,
)
from core.db.session import get_session
from core.gateway.channels.registry import outbound_adapter_for
from core.gateway.message_splitter import split_for_bubbles
from core.shared.crypto import get_secret_box
from core.shared.events import ON_MESSAGE_SENT, event_bus
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

BUBBLE_DELAY_S = 0.7
SEND_RETRIES = 2


async def _resolve_whatsapp_credentials(
    meta_connection_id: uuid.UUID, connection: MetaConnection
) -> dict[str, str] | None:
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND

    async with get_session() as session:
        phone = (
            await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == meta_connection_id))
        ).scalars().first()
    if phone is None:
        return None
    token = await meta_token_store.get_token(
        meta_connection_id, TOKEN_KIND, connection.business_id or str(meta_connection_id), tenant_id=connection.tenant_id
    )
    if not token:
        return None
    return {"phone_number_id": phone.phone_number_id, "access_token": token}


async def _resolve_messenger_credentials(
    meta_connection_id: uuid.UUID, connection: MetaConnection, conversation: Conversation
) -> dict[str, str] | None:
    """Página de Messenger: token vía `meta_token_store` (kind=
    'page_access'), más el estado que `meta_messenger.send_messenger_message`
    necesita para validar ventana de servicio/Conversation Routing ANTES de
    llamar a Meta (viaja en el dict de credenciales, ver
    `channels/messenger/outbound.py`).

    Una `MetaConnection` de Messenger puede agrupar varias `FbPage` (se
    conectan todas las manejables en un solo Facebook Login) -- se resuelve
    la Página EXACTA vía `conversation.fb_page_id` (hallazgo
    auditor-multitenant, 2026-09-23: tomar la primera de la conexión podía
    responder "como" la Página equivocada si el tenant tiene más de una).
    Fallback a la primera solo para conversaciones viejas sin
    `fb_page_id` poblado (anteriores a esta corrección)."""
    from core.gateway import meta_token_store
    from core.gateway.meta_messenger import PAGE_TOKEN_KIND

    async with get_session() as session:
        if conversation.fb_page_id is not None:
            page = await session.get(FbPage, conversation.fb_page_id)
            if page is not None and (page.connection_id != meta_connection_id or page.tenant_id != connection.tenant_id):
                page = None  # no coincide con lo esperado: no confiar, cae al fallback
        else:
            page = None
        if page is None:
            page = (
                await session.execute(
                    select(FbPage).where(FbPage.connection_id == meta_connection_id, FbPage.tenant_id == connection.tenant_id)
                )
            ).scalars().first()
    if page is None:
        return None
    token = await meta_token_store.get_token(meta_connection_id, PAGE_TOKEN_KIND, page.page_id, tenant_id=connection.tenant_id)
    if not token:
        return None
    return {
        "page_id": page.page_id,
        "access_token": token,
        "connection_id": str(meta_connection_id),
        "graph_api_version": connection.graph_api_version,
        "service_window_expires_at": conversation.service_window_expires_at.isoformat() if conversation.service_window_expires_at else "",
        "conversation_routing_enabled": "true" if page.conversation_routing_enabled else "",
    }


async def _resolve_instagram_credentials(
    meta_connection_id: uuid.UUID, connection: MetaConnection, conversation: Conversation
) -> dict[str, str] | None:
    """Resuelve la `IgAccount` EXACTA vía `conversation.ig_account_id`
    (mismo criterio que `fb_page_id` -- una `MetaConnection` de Instagram
    puede agrupar varias cuentas). Según `login_route`: ruta (a) reusa el
    token de la `FbPage` vinculada (`linked_page_id`, kind `page_access`,
    mismo mecanismo que Messenger); ruta (b) usa el token propio de la
    cuenta (kind `long_lived_user`, subject_id=`ig_user_id`)."""
    from core.gateway import meta_token_store
    from core.gateway.meta_instagram import IG_LONG_LIVED_TOKEN_KIND
    from core.gateway.meta_messenger import PAGE_TOKEN_KIND

    async with get_session() as session:
        account = None
        if conversation.ig_account_id is not None:
            account = await session.get(IgAccount, conversation.ig_account_id)
            if account is not None and (account.connection_id != meta_connection_id or account.tenant_id != connection.tenant_id):
                account = None
        if account is None:
            account = (
                await session.execute(
                    select(IgAccount).where(IgAccount.connection_id == meta_connection_id, IgAccount.tenant_id == connection.tenant_id)
                )
            ).scalars().first()
    if account is None:
        return None

    base = {
        "login_route": account.login_route or "",
        "connection_id": str(meta_connection_id),
        "graph_api_version": connection.graph_api_version,
        "service_window_expires_at": conversation.service_window_expires_at.isoformat() if conversation.service_window_expires_at else "",
    }
    if account.login_route == "ig_facebook_login":
        if not account.linked_page_id:
            return None
        token = await meta_token_store.get_token(meta_connection_id, PAGE_TOKEN_KIND, account.linked_page_id, tenant_id=connection.tenant_id)
        if not token:
            return None
        return {**base, "page_id": account.linked_page_id, "access_token": token}

    token = await meta_token_store.get_token(meta_connection_id, IG_LONG_LIVED_TOKEN_KIND, account.ig_user_id, tenant_id=connection.tenant_id)
    if not token:
        return None
    return {**base, "ig_user_id": account.ig_user_id, "access_token": token}


async def _resolve_meta_connection_credentials(
    meta_connection_id: uuid.UUID, expected_tenant_id: uuid.UUID | None, conversation: Conversation | None = None
) -> dict[str, str] | None:
    """Conexiones creadas por el wizard de Embedded Signup (ADR-0030): no
    tienen `ChannelConnection`, el token vive en `meta_token_store`.

    `expected_tenant_id` (el tenant de la CONVERSACIÓN que se está
    despachando, no un contextvar ambiente) se compara contra
    `connection.tenant_id` antes de pedir el token -- segundo cinturón más
    allá de RLS/unicidad de esquema (hallazgo auditor-multitenant,
    2026-09-22: `phone_numbers.phone_number_id` es único global, no
    compuesto con `tenant_id`; si esa garantía de esquema se relajara algún
    día, esta aserción evita enviar con el token de OTRO tenant). Dispatch
    por `MetaConnection.channel`: WhatsApp vía `PhoneNumber`, Messenger vía
    `FbPage` -- credenciales incompatibles entre sí, no se pueden resolver
    con una única consulta genérica."""
    async with get_session() as session:
        connection = await session.get(MetaConnection, meta_connection_id)
    if connection is None:
        return None
    if expected_tenant_id is not None and connection.tenant_id != expected_tenant_id:
        log.error(
            "meta_connection_tenant_mismatch",
            meta_connection_id=str(meta_connection_id),
            connection_tenant_id=str(connection.tenant_id),
            expected_tenant_id=str(expected_tenant_id),
        )
        return None

    if connection.channel == "messenger" and conversation is not None:
        return await _resolve_messenger_credentials(meta_connection_id, connection, conversation)
    if connection.channel == "instagram" and conversation is not None:
        return await _resolve_instagram_credentials(meta_connection_id, connection, conversation)
    return await _resolve_whatsapp_credentials(meta_connection_id, connection)


async def resolve_credentials(
    connection_id: uuid.UUID | None,
    meta_connection_id: uuid.UUID | None = None,
    expected_tenant_id: uuid.UUID | None = None,
    conversation: Conversation | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Devuelve (provider, channel, credentials). Orden: `ChannelConnection`
    (conexión manual) -> `MetaConnection`+`meta_token_store` (wizard de
    Embedded Signup, ADR-0030) -> conexión primaria en BD -> `.env`.

    `expected_tenant_id`: el tenant de la conversación que se está
    despachando -- se verifica contra `MetaConnection.tenant_id` antes de
    usar sus credenciales (ver docstring de `_resolve_meta_connection_credentials`)."""
    settings = get_settings()
    if connection_id is not None:
        async with get_session() as session:
            connection = await session.get(ChannelConnection, connection_id)
        if connection and connection.credentials_encrypted:
            creds = get_secret_box().decrypt_json(connection.credentials_encrypted)
            return connection.provider, connection.channel, {str(k): str(v) for k, v in creds.items()}

    if meta_connection_id is not None:
        creds = await _resolve_meta_connection_credentials(meta_connection_id, expected_tenant_id, conversation)
        if creds:
            channel = conversation.channel if conversation is not None else "whatsapp"
            return "meta", channel, creds

    # Fallback: conexión primaria en BD
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection).where(
                ChannelConnection.active.is_(True), ChannelConnection.is_primary.is_(True)
            )
        )
        primary = result.scalar_one_or_none()
    if primary and primary.credentials_encrypted:
        creds = get_secret_box().decrypt_json(primary.credentials_encrypted)
        return primary.provider, primary.channel, {str(k): str(v) for k, v in creds.items()}

    # Último fallback: .env
    return "meta", "whatsapp", {
        "phone_number_id": settings.whatsapp_phone_number_id,
        "access_token": settings.whatsapp_access_token,
    }


async def send_to_conversation(
    conversation_id: uuid.UUID,
    text: str,
    image_url: str | None = None,
    sender_type: str = "agent",
) -> bool:
    """Envía la respuesta al canal. Devuelve True si el envío fue exitoso.

    El mensaje outbound SIEMPRE se persiste (status 'sent' | 'failed') para que
    un envío roto quede auditado en BD y pueda reintentarse/escalarse, en vez de
    descartarse en silencio (incidente de envío silencioso detectado en producción).
    """
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return False
        customer = await session.get(Customer, conversation.customer_id)
        if customer is None:
            return False

    provider, channel, credentials = await resolve_credentials(
        conversation.connection_id, conversation.meta_connection_id,
        expected_tenant_id=conversation.tenant_id, conversation=conversation,
    )
    adapter = outbound_adapter_for(provider, channel)

    from core.gateway.meta_usage import connection_id_for_phone_number, is_paused

    current_tenant_id = get_current_tenant_id()
    meta_connection_id = await connection_id_for_phone_number(
        credentials.get("phone_number_id", ""), current_tenant_id
    )
    if await is_paused(current_tenant_id, meta_connection_id):
        log.error(
            "envio_pausado_por_uso_de_api",
            conversation_id=str(conversation_id),
            connection_id=str(meta_connection_id),
        )
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=get_current_tenant_id(),
                    conversation_id=conversation_id,
                    direction="outbound",
                    sender_type=sender_type,
                    content=text,
                    status="failed",
                )
            )
        return False

    settings = get_settings()
    style = str(
        (await _behavior_config()).get("split_style", settings.message_split_style)
    )
    bubbles = split_for_bubbles(text, style)

    delivered = True
    for index, bubble in enumerate(bubbles):
        is_last = index == len(bubbles) - 1
        for attempt in range(SEND_RETRIES):
            try:
                if is_last and image_url:
                    await adapter.send_image(credentials, customer.external_id, image_url, bubble)
                else:
                    await adapter.send_text(credentials, customer.external_id, bubble)
                break
            except Exception:
                log.exception("bubble_send_failed", attempt=attempt + 1,
                              conversation_id=str(conversation_id))
                if attempt + 1 >= SEND_RETRIES:
                    delivered = False  # no seguir enviando burbujas desordenadas
                    break
                await asyncio.sleep(1)
        if not is_last and delivered:
            await asyncio.sleep(BUBBLE_DELAY_S)

    # Persistir el turno saliente SIEMPRE (con status de entrega)
    async with get_session() as session:
        session.add(
            Message(
                tenant_id=get_current_tenant_id(),
                conversation_id=conversation_id,
                direction="outbound",
                sender_type=sender_type,
                content=text,
                status="sent" if delivered else "failed",
            )
        )
    await event_bus.emit(ON_MESSAGE_SENT, conversation_id=conversation_id, content=text)
    return delivered


async def _behavior_config() -> dict[str, object]:
    from core.db.models import AppSettings

    async with get_session() as session:
        result = await session.execute(
            select(AppSettings).where(
                AppSettings.tenant_id == get_current_tenant_id(),
                AppSettings.deleted_at.is_(None),
            )
        )
        app_settings = result.scalar_one_or_none()
        return dict(app_settings.behavior_config) if app_settings else {}

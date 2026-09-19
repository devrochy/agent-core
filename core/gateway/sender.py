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


async def _resolve_meta_connection_credentials(
    meta_connection_id: uuid.UUID, expected_tenant_id: uuid.UUID | None
) -> dict[str, str] | None:
    """Conexiones creadas por el wizard de Embedded Signup (ADR-0030): no
    tienen `ChannelConnection`, el token vive en `meta_token_store`.

    `expected_tenant_id` (el tenant de la CONVERSACIÓN que se está
    despachando, no un contextvar ambiente) se compara contra
    `connection.tenant_id` antes de pedir el token -- segundo cinturón más
    allá de RLS/unicidad de esquema (hallazgo auditor-multitenant,
    2026-09-22: `phone_numbers.phone_number_id` es único global, no
    compuesto con `tenant_id`; si esa garantía de esquema se relajara algún
    día, esta aserción evita enviar con el token de OTRO tenant)."""
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND

    async with get_session() as session:
        connection = await session.get(MetaConnection, meta_connection_id)
        phone = (
            await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == meta_connection_id))
        ).scalars().first()
    if connection is None or phone is None:
        return None
    if expected_tenant_id is not None and connection.tenant_id != expected_tenant_id:
        log.error(
            "meta_connection_tenant_mismatch",
            meta_connection_id=str(meta_connection_id),
            connection_tenant_id=str(connection.tenant_id),
            expected_tenant_id=str(expected_tenant_id),
        )
        return None
    token = await meta_token_store.get_token(
        meta_connection_id, TOKEN_KIND, connection.business_id or str(meta_connection_id), tenant_id=connection.tenant_id
    )
    if not token:
        return None
    return {"phone_number_id": phone.phone_number_id, "access_token": token}


async def resolve_credentials(
    connection_id: uuid.UUID | None,
    meta_connection_id: uuid.UUID | None = None,
    expected_tenant_id: uuid.UUID | None = None,
) -> tuple[str, dict[str, str]]:
    """Devuelve (provider, credentials). Orden: `ChannelConnection` (conexión
    manual) -> `MetaConnection`+`meta_token_store` (wizard de Embedded
    Signup, ADR-0030) -> conexión primaria en BD -> `.env`.

    `expected_tenant_id`: el tenant de la conversación que se está
    despachando -- se verifica contra `MetaConnection.tenant_id` antes de
    usar sus credenciales (ver docstring de `_resolve_meta_connection_credentials`)."""
    settings = get_settings()
    if connection_id is not None:
        async with get_session() as session:
            connection = await session.get(ChannelConnection, connection_id)
        if connection and connection.credentials_encrypted:
            creds = get_secret_box().decrypt_json(connection.credentials_encrypted)
            return connection.provider, {str(k): str(v) for k, v in creds.items()}

    if meta_connection_id is not None:
        creds = await _resolve_meta_connection_credentials(meta_connection_id, expected_tenant_id)
        if creds:
            return "meta", creds

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
        return primary.provider, {str(k): str(v) for k, v in creds.items()}

    # Último fallback: .env
    return "meta", {
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

    provider, credentials = await resolve_credentials(
        conversation.connection_id, conversation.meta_connection_id, expected_tenant_id=conversation.tenant_id
    )
    adapter = outbound_adapter_for(provider)

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

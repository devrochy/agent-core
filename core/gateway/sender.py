"""Envío de respuestas: resuelve conexión, divide en burbujas y ENCOLA cada
burbuja como una tarea Celery de envío (`core/tasks/outbound_send.py`),
que corre en el proceso `celery-worker` -- separado del proceso que
atiende webhooks/HTTP.

- Burbujas: cada una es un `Message` propio con su propio
  `client_message_id` -- reintentar el envío de UNA burbuja nunca reenvía
  las demás.
- Credenciales: channel_connections (cifradas) o fallback a .env --
  resueltas de nuevo DENTRO de la tarea (que puede correr en otro proceso),
  no acá.
- `send_to_conversation` devuelve `True` si las burbujas se ENCOLARON
  correctamente -- no si Meta ya las entregó (eso lo decide la tarea,
  asíncrona). Un fallo de envío real dispara `escalate()` desde la propia
  tarea, no desde acá.
"""

from __future__ import annotations

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
    utcnow,
)
from core.db.session import get_session
from core.gateway.message_splitter import split_for_bubbles
from core.shared.crypto import get_secret_box
from core.shared.events import ON_MESSAGE_SENT, event_bus
from core.shared.exceptions import OutboundWindowExpiredError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


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
        if connection is None:
            return None
        # Filtro explícito de tenant además de RLS (defensa en profundidad,
        # invariante nº1): el número nunca se lee sin atarlo a su conexión
        # (mismo criterio que `meta_byok.save_byok_connection`).
        phone = (
            await session.execute(
                select(PhoneNumber).where(
                    PhoneNumber.connection_id == meta_connection_id,
                    PhoneNumber.tenant_id == connection.tenant_id,
                )
            )
        ).scalars().first()
    if phone is None:
        return None
    if expected_tenant_id is not None and connection.tenant_id != expected_tenant_id:
        log.error(
            "meta_connection_tenant_mismatch",
            meta_connection_id=str(meta_connection_id),
            connection_tenant_id=str(connection.tenant_id),
            expected_tenant_id=str(expected_tenant_id),
        )
        return None
    # El token vive bajo distinto `kind`/`subject` según el flujo que creó la
    # conexión: BYOK (`save_byok_connection`) usa `cloud_api_access_token` con
    # `subject=meta_app_id`; Embedded Signup usa `TOKEN_KIND`
    # (`business_integration_system_user`) con `subject=business_id` (o el id de
    # la conexión). Se prueban ambos: con el kind/subject equivocado, un envío
    # BYOK no encontraba su token y caía al fallback del `.env`
    # (otro número/credenciales).
    from core.gateway.meta_byok import CLOUD_API_ACCESS_TOKEN_KIND

    token = await meta_token_store.get_token(
        meta_connection_id, CLOUD_API_ACCESS_TOKEN_KIND, connection.meta_app_id, tenant_id=connection.tenant_id
    )
    if not token:
        token = await meta_token_store.get_token(
            meta_connection_id,
            TOKEN_KIND,
            connection.business_id or str(meta_connection_id),
            tenant_id=connection.tenant_id,
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
        # `tenant_id` explícito además de RLS -- defensa en profundidad,
        # hallazgo auditor-multitenant 2026-09-20: sin este chequeo, una
        # RLS mal configurada en `get_session()` dejaría leer credenciales
        # cifradas de OTRO tenant a partir de un `connection_id` ajeno.
        if connection is not None and expected_tenant_id is not None and connection.tenant_id != expected_tenant_id:
            log.error(
                "channel_connection_tenant_mismatch",
                connection_id=str(connection_id),
                connection_tenant_id=str(connection.tenant_id),
                expected_tenant_id=str(expected_tenant_id),
            )
            connection = None
        if connection and connection.credentials_encrypted:
            creds = get_secret_box().decrypt_json(connection.credentials_encrypted)
            return connection.provider, {str(k): str(v) for k, v in creds.items()}

    if meta_connection_id is not None:
        creds = await _resolve_meta_connection_credentials(meta_connection_id, expected_tenant_id)
        if creds:
            return "meta", creds

    # Fallback: conexión primaria en BD -- `tenant_id` explícito además de
    # RLS (defensa en profundidad, mismo criterio que el resto del código
    # de esta serie): sin este filtro, dos tenants con conexión primaria
    # activa hacen que `scalar_one_or_none` reviente con
    # `MultipleResultsFound` en vez de devolver la propia.
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection).where(
                ChannelConnection.active.is_(True),
                ChannelConnection.is_primary.is_(True),
                ChannelConnection.tenant_id == get_current_tenant_id(),
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


def validate_service_window(conversation: Conversation, *, is_template: bool) -> None:
    """Ventana de 24h de WhatsApp: fuera de ventana solo se puede enviar una
    plantilla. Se llama ANTES de encolar -- nunca gasta un ciclo de cola/
    rate-limit en un envío libre que Meta va a rechazar seguro (131047)."""
    if is_template:
        return
    expires_at = conversation.service_window_expires_at
    if expires_at is not None and expires_at < utcnow():
        raise OutboundWindowExpiredError(
            "La ventana de servicio de 24h venció -- reenviá como plantilla"
        )


async def send_to_conversation(
    conversation_id: uuid.UUID,
    text: str,
    image_url: str | None = None,
    sender_type: str = "agent",
    *,
    is_template: bool = False,
) -> bool:
    """Encola el envío al canal (cada burbuja, su propia tarea Celery).
    Devuelve True si se encoló correctamente -- NO si Meta ya lo entregó
    (eso lo resuelve la tarea de forma asíncrona, ver
    `core/tasks/outbound_send.py`).

    El mensaje outbound SIEMPRE se persiste (status 'queued' -> 'sent'/
    'failed' más tarde) para que un envío roto quede auditado en BD y
    pueda reintentarse/escalarse, en vez de descartarse en silencio
    (incidente de envío silencioso detectado en producción).
    """
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return False
        customer = await session.get(Customer, conversation.customer_id)
        if customer is None:
            return False

    try:
        validate_service_window(conversation, is_template=is_template)
    except OutboundWindowExpiredError as exc:
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=get_current_tenant_id(), conversation_id=conversation_id,
                    direction="outbound", sender_type=sender_type, content=text,
                    status="failed", error_reason=str(exc),
                )
            )
        return False

    _provider, credentials = await resolve_credentials(
        conversation.connection_id, conversation.meta_connection_id, expected_tenant_id=conversation.tenant_id
    )

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
                    error_reason="Envío pausado: uso de la API de Meta cerca del límite (>=90%)",
                )
            )
        return False

    settings = get_settings()
    style = str(
        (await _behavior_config()).get("split_style", settings.message_split_style)
    )
    bubbles = split_for_bubbles(text, style)

    from core.tasks.outbound_send import outbound_send_task

    for index, bubble in enumerate(bubbles):
        is_last = index == len(bubbles) - 1
        client_message_id = str(uuid.uuid4())
        async with get_session() as session:
            message = Message(
                tenant_id=current_tenant_id,
                conversation_id=conversation_id,
                direction="outbound",
                sender_type=sender_type,
                content=bubble,
                status="queued",
                client_message_id=client_message_id,
                media_type="image" if (is_last and image_url) else None,
                media_url=image_url if (is_last and image_url) else None,
            )
            session.add(message)
            await session.flush()
            message_id = message.id

        outbound_send_task.delay(str(message_id), str(current_tenant_id))

    await event_bus.emit(ON_MESSAGE_SENT, conversation_id=conversation_id, content=text)
    return True


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

"""Alta manual de una conexión de WhatsApp con la app PROPIA del tenant
(BYOK -- Bring Your Own App/Key), a diferencia del wizard de Embedded
Signup (`meta_onboarding.py`), que usa la app del framework vía OAuth.

`meta_preflight.resolve_app_credentials` ya sabía LEER de este camino
(`MetaConnection.meta_app_id/meta_config_id` + `meta_token_store` con
`kind="app_access"`) desde que se introdujo, pero hasta este módulo nada
lo escribía. El número de prueba gratis de Meta vive dentro de la app que
el propio tenant crea -- ya viene registrado (pestaña "API Setup" del
producto WhatsApp), por eso acá no hace falta el paso de PIN de
`register_number_step`.

`CLOUD_API_ACCESS_TOKEN_KIND` es un `kind` nuevo de `meta_token_store`
(distinto de `business_integration_system_user`, que es semánticamente
el token que emite el flujo OAuth de Embedded Signup) -- no hay ningún
candado de `kind` en el store, así que reusarlo con un string propio es
seguro.

`WEBHOOK_VERIFY_TOKEN_KIND`/`resolve_webhook_verify_token`: por defecto
el handshake de `core/gateway/webhooks.py` usa `settings.whatsapp_
verify_token` (global). Si el admin generó/escribió uno propio en el
paso de webhook del wizard, se guarda acá y `webhooks.py` lo prioriza
antes de caer al global -- opcional, dejarlo vacío no cambia nada del
comportamiento actual.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.db.models import MetaConnection, PhoneNumber, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.connections import verify_meta_credentials
from core.shared.exceptions import AgentCoreError

APP_SECRET_KIND = "app_access"
CLOUD_API_ACCESS_TOKEN_KIND = "cloud_api_access_token"
WEBHOOK_VERIFY_TOKEN_KIND = "webhook_verify_token"


class ByokError(AgentCoreError):
    """Credenciales BYOK rechazadas por Meta o incompletas."""


async def save_byok_connection(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID | None,
    app_id: str,
    app_secret: str,
    phone_number_id: str,
    access_token: str,
    environment: str = "sandbox",
    verify_token: str | None = None,
) -> MetaConnection:
    """Verifica las credenciales contra Graph API y recién después
    persiste -- nunca guarda a ciegas. Reentrante: volver a correrlo con
    el mismo `app_id` actualiza la conexión existente en vez de duplicar
    (respeta `uq_meta_connections_tenant_app`)."""
    if not app_id or not app_secret:
        raise ByokError("Faltan App ID o App Secret")

    ok, message, info = await verify_meta_credentials(phone_number_id, access_token, app_secret)
    if not ok:
        raise ByokError(message)

    async with get_session() as session:
        connection = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "whatsapp",
                    MetaConnection.environment == environment,
                    MetaConnection.meta_app_id == app_id,
                )
            )
        ).scalar_one_or_none()

        if connection is None:
            connection = MetaConnection(
                tenant_id=tenant_id, channel="whatsapp", environment=environment, meta_app_id=app_id,
                status="pending",
            )
            session.add(connection)
            await session.flush()
        else:
            # Estado intermedio hasta confirmar que los dos tokens se
            # guardaron -- recién al final se marca "connected" (ver abajo).
            connection.status = "pending"
        connection.status_reason = None
        connection.connected_by = admin_id
        connection.connected_at = utcnow()
        connection_id = connection.id

        # Defensa en profundidad: `connection_id` ya nació filtrado por
        # `tenant_id` arriba, pero el filtro explícito acá evita que un
        # futuro refactor que reciba `connection_id` de otro origen abra
        # una fuga entre tenants (mismo criterio que `meta_token_store`).
        phone = (
            await session.execute(
                select(PhoneNumber).where(
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.connection_id == connection_id,
                    PhoneNumber.phone_number_id == phone_number_id,
                )
            )
        ).scalar_one_or_none()
        if phone is None:
            phone = PhoneNumber(tenant_id=tenant_id, connection_id=connection_id, phone_number_id=phone_number_id)
            session.add(phone)
        phone.display_phone_number = info.get("display_phone_number")
        phone.verified_name = info.get("verified_name")
        phone.registered = True

    try:
        await meta_token_store.put_token(connection_id, APP_SECRET_KIND, app_id, app_secret, tenant_id=tenant_id)
        await meta_token_store.put_token(
            connection_id, CLOUD_API_ACCESS_TOKEN_KIND, app_id, access_token, tenant_id=tenant_id
        )
        if verify_token:
            await meta_token_store.put_token(
                connection_id, WEBHOOK_VERIFY_TOKEN_KIND, app_id, verify_token, tenant_id=tenant_id
            )
    except Exception as exc:
        # Las credenciales YA fueron verificadas contra Graph -- este
        # except cubre un fallo de infraestructura al guardar el token
        # (no credenciales inválidas), y evita dejar la conexión en
        # "connected" sin secretos reales.
        async with get_session() as session:
            db_connection = await session.get(MetaConnection, connection_id)
            if db_connection is not None:
                db_connection.status = "error"
                db_connection.status_reason = f"Fallo guardando credenciales: {exc}"[:250]
        raise

    async with get_session() as session:
        db_connection = await session.get(MetaConnection, connection_id)
        db_connection.status = "connected"
        return db_connection


async def resolve_webhook_verify_token(tenant_id: uuid.UUID) -> str | None:
    """`None` si el tenant no tiene una `MetaConnection` de WhatsApp
    conectada, o si no guardó un verify_token propio -- en ambos casos
    `webhooks.py` cae a `settings.whatsapp_verify_token` (comportamiento
    sin cambios). Filtra por `tenant_id` explícito, nunca confía solo en
    RLS (mismo criterio que el resto del módulo)."""
    async with get_session() as session:
        connection = (
            await session.execute(
                select(MetaConnection)
                .where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "whatsapp",
                    MetaConnection.status == "connected",
                )
                .order_by(MetaConnection.connected_at.desc())
            )
        ).scalars().first()
    if connection is None:
        return None
    return await meta_token_store.get_token(
        connection.id, WEBHOOK_VERIFY_TOKEN_KIND, connection.meta_app_id, tenant_id=tenant_id
    )

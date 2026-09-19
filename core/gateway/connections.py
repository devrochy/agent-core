"""Conexiones de canal: consulta y resolución de credenciales.

Una conexión vive en `channel_connections` con sus credenciales cifradas
(Fernet). El webhook y el sender las resuelven desde aquí con fallback al
.env (el caso "recién instalado, sin configurar en el panel").
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from core.config import get_settings
from core.db.models import ChannelConnection, utcnow
from core.db.session import get_session
from core.shared.crypto import get_secret_box
from core.shared.tenant import get_current_tenant_id

# Claves que se guardan cifradas en credentials_encrypted (lista blanca)
CREDENTIAL_KEYS = ("phone_number_id", "app_secret", "access_token", "verify_token")


async def list_connections() -> list[ChannelConnection]:
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection)
            .where(ChannelConnection.deleted_at.is_(None))
            .order_by(ChannelConnection.created_at)
        )
        return list(result.scalars().all())


async def get_connection(connection_id: uuid.UUID) -> ChannelConnection | None:
    async with get_session() as session:
        return await session.get(ChannelConnection, connection_id)


async def find_by_external_id(external_id: str) -> ChannelConnection | None:
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection).where(
                ChannelConnection.external_id == external_id,
                ChannelConnection.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()


async def primary_connection() -> ChannelConnection | None:
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection)
            .where(
                ChannelConnection.active.is_(True),
                ChannelConnection.is_primary.is_(True),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


def decrypt_credentials(connection: ChannelConnection) -> dict[str, str]:
    if not connection.credentials_encrypted:
        return {}
    raw = get_secret_box().decrypt_json(connection.credentials_encrypted)
    return {str(k): str(v) for k, v in raw.items() if v}


async def resolve_credentials_for_external(external_id: str) -> dict[str, str] | None:
    """Credenciales de la conexión que matchea el phone_number_id del payload."""
    connection = await find_by_external_id(external_id)
    if connection is None or not connection.active:
        return None
    return decrypt_credentials(connection)


async def resolve_primary_credentials() -> dict[str, str] | None:
    """Credenciales de la conexión primaria activa, o None si no hay."""
    connection = await primary_connection()
    if connection is None:
        return None
    return decrypt_credentials(connection)


def env_credentials() -> dict[str, str]:
    settings = get_settings()
    return {
        "phone_number_id": settings.whatsapp_phone_number_id,
        "app_secret": settings.whatsapp_app_secret,
        "access_token": settings.whatsapp_access_token,
        "verify_token": settings.whatsapp_verify_token,
    }


async def effective_credentials(external_id: str = "") -> dict[str, str]:
    """Orden de resolución: conexión por external_id -> primaria activa -> .env."""
    if external_id:
        creds = await resolve_credentials_for_external(external_id)
        if creds:
            return creds
    creds = await resolve_primary_credentials()
    if creds:
        return creds
    return env_credentials()


async def save_connection(
    label: str,
    credentials: dict[str, str],
    connection_id: uuid.UUID | None = None,
) -> tuple[ChannelConnection, bool]:
    """Crea o actualiza una conexión Meta/WhatsApp. Devuelve (conn, created).

    La primera conexión creada queda como primaria automáticamente.
    """
    box = get_secret_box()
    phone_number_id = credentials.get("phone_number_id", "")
    encrypted = box.encrypt_json({k: credentials.get(k, "") for k in CREDENTIAL_KEYS})

    async with get_session() as session:
        if connection_id is not None:
            connection = await session.get(ChannelConnection, connection_id)
            if connection is None:
                raise ValueError("La conexión no existe")
            connection.label = label
            connection.external_id = phone_number_id or connection.external_id
            connection.credentials_encrypted = encrypted
            connection.active = True
            created = False
        else:
            existing = (
                await session.execute(
                    select(ChannelConnection).where(
                        ChannelConnection.external_id == phone_number_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.label = label
                existing.credentials_encrypted = encrypted
                existing.active = True
                existing.deleted_at = None  # re-agregar des-elimina (invariante #8)
                return existing, False
            connection = ChannelConnection(
                tenant_id=get_current_tenant_id(),
                channel="whatsapp",
                provider="meta",
                label=label,
                active=True,
                is_primary=False,
                external_id=phone_number_id,
                credentials_encrypted=encrypted,
            )
            session.add(connection)
            await session.flush()
            created = True

        # La primera conexión activa queda como primaria
        count_active = (
            await session.execute(
                select(func.count(ChannelConnection.id)).where(ChannelConnection.active.is_(True))
            )
        ).scalar() or 0
        if created and count_active <= 1:
            connection.is_primary = True
        return connection, created


async def set_active(connection_id: uuid.UUID, active: bool) -> None:
    async with get_session() as session:
        connection = await session.get(ChannelConnection, connection_id)
        if connection is None:
            return
        connection.active = active
        if not active:
            connection.is_primary = False


async def set_primary(connection_id: uuid.UUID) -> None:
    async with get_session() as session:
        rows = (await session.execute(select(ChannelConnection))).scalars().all()
        for row in rows:
            row.is_primary = row.id == connection_id
            if row.id == connection_id:
                row.active = True


async def delete_connection(connection_id: uuid.UUID) -> None:
    """Borra (soft-delete) una conexión — invariante #8: sin borrado físico."""
    async with get_session() as session:
        connection = await session.get(ChannelConnection, connection_id)
        if connection is not None:
            connection.deleted_at = utcnow()
            connection.active = False


async def verify_meta_credentials(
    phone_number_id: str, access_token: str, app_secret: str
) -> tuple[bool, str, dict[str, Any]]:
    """Valida las credenciales contra Meta Graph API antes de guardar.

    Devuelve (ok, mensaje, info). `info` incluye verified_name y
    display_phone_number reportados por Meta (ag-sale: no confiar en lo que
    tipea el admin; la clave de ruteo la reporta el proveedor cuando puede).

    Usa el cliente único del panel (`core/gateway/meta_graph.py`): agrega
    `appsecret_proof` y registra la llamada en `graph_api_calls`/`graph_api_usage`.
    """
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_graph import graph_fetch

    if not phone_number_id or not access_token or not app_secret:
        return False, "Faltan phone_number_id, access_token o app_secret", {}

    try:
        info = await graph_fetch(
            f"/{phone_number_id}",
            token=access_token,
            app_secret=app_secret,
            query={"fields": "id,verified_name,display_phone_number"},
        )
    except GraphError as exc:
        return False, f"Meta rechazó las credenciales: {exc.message}", {}
    except Exception as exc:
        return False, f"Error de red con Meta: {str(exc)[:120]}", {}

    return True, "Credenciales verificadas con Meta", info or {}
"""Conexiones de canal: consulta y resolución de credenciales.

Una conexión vive en `channel_connections` con sus credenciales cifradas
(Fernet). El webhook y el sender las resuelven desde aquí con fallback al
.env (el caso "recién instalado, sin configurar en el panel").

Las filas con `meta_connection_id` son la conexión canónica de un número de Meta
(ADR-0115): no tienen credenciales propias y las administra el alta de Meta. Las de tipo
`webchat` son el widget de chat web y las administra Configuración → Chat web
(`chat_web_conexion`). Este módulo es el de las conexiones MANUALES, así que no lista, no
resuelve y no edita ninguna de las dos (`ES_MANUAL`, `administrada_aparte`).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from core.config import get_settings
from core.db.models import ChannelConnection, utcnow
from core.db.session import get_session
from core.gateway.connection_types import (
    CATEGORY_COMMUNICATION,
    WEBCHAT,
    WHATSAPP_META,
    external_id_for,
    get_connection_type,
    normalize_credentials,
)
from core.shared.crypto import get_secret_box
from core.shared.tenant import get_current_tenant_id

# Conexión sin cuenta de Meta y que no es el chat web: manual, `waweb`, correo, calendario
# (ADR-0115). `type_id` NULL (filas legacy) cuenta como manual.
ES_MANUAL = and_(
    ChannelConnection.meta_connection_id.is_(None),
    or_(ChannelConnection.type_id.is_(None), ChannelConnection.type_id != WEBCHAT),
)


def administrada_aparte(connection: ChannelConnection) -> bool:
    """La conexión no es manual: la canónica de un número de Meta (la administra su cuenta) o
    el chat web (Configuración → Chat web). La API y el panel de conexiones manuales responden
    404 para ellas."""
    return connection.meta_connection_id is not None or connection.type_id == WEBCHAT


async def list_connections() -> list[ChannelConnection]:
    """Filtra por `tenant_id` explícito además de RLS (defensa en
    profundidad, hallazgo del auditor-multitenant -- deuda preexistente
    corregida al retocar `/admin/conexiones`)."""
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection)
            .where(
                ChannelConnection.deleted_at.is_(None),
                ChannelConnection.tenant_id == get_current_tenant_id(),
                ES_MANUAL,
            )
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
                ES_MANUAL,
            )
        )
        return result.scalar_one_or_none()


async def primary_connection(tenant_id: uuid.UUID | None = None) -> ChannelConnection | None:
    """Conexión primaria activa de la categoría de mensajería, o None.

    La primacía solo aplica a `comunicacion` (ADR-0069): una conexión de correo o
    calendario nunca es el fallback de credenciales del sender de mensajería. Las
    filas legacy sin `category` (insertadas por tests) se tratan como mensajería.
    """
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection)
            .where(
                ChannelConnection.active.is_(True),
                ChannelConnection.is_primary.is_(True),
                # Filtro EXPLÍCITO de tenant además de RLS (defensa en
                # profundidad, invariante nº1): `tid is None` no matchea nada
                # (fail-closed), nunca "sin filtrar".
                ChannelConnection.tenant_id == tid,
                ES_MANUAL,
                or_(
                    ChannelConnection.category == CATEGORY_COMMUNICATION,
                    ChannelConnection.category.is_(None),
                ),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


def decrypt_credentials(connection: ChannelConnection) -> dict[str, str]:
    if not connection.credentials_encrypted:
        return {}
    raw = get_secret_box().decrypt_json(connection.credentials_encrypted)
    return {str(k): str(v) for k, v in raw.items() if v}


def _decrypt_raw(connection: ChannelConnection) -> dict[str, Any]:
    """Descifra credenciales conservando los tipos (int/bool) del schema."""
    if not connection.credentials_encrypted:
        return {}
    return get_secret_box().decrypt_json(connection.credentials_encrypted)


def credentials_of(connection: ChannelConnection) -> dict[str, Any]:
    """Credenciales descifradas de una conexión (para prellenar el form de edición)."""
    return _decrypt_raw(connection)


async def active_connection_type_ids(tenant_id: uuid.UUID | None = None) -> set[str]:
    """`type_id` de las conexiones activas del tenant (gating ADR-0072)."""
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    async with get_session() as session:
        rows = await session.execute(
            select(ChannelConnection.type_id).where(
                ChannelConnection.tenant_id == tid,
                ChannelConnection.active.is_(True),
                ChannelConnection.deleted_at.is_(None),
                ChannelConnection.type_id.is_not(None),
                # El gating mira las conexiones manuales, como antes de ADR-0115.
                ES_MANUAL,
            )
        )
    return {str(t) for t in rows.scalars().all() if t}


async def get_active_connection(
    type_id: str, tenant_id: uuid.UUID | None = None
) -> ChannelConnection | None:
    """Conexión activa del tipo pedido para el tenant (la primaria primero)."""
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    async with get_session() as session:
        result = await session.execute(
            select(ChannelConnection)
            .where(
                ChannelConnection.type_id == type_id,
                ChannelConnection.tenant_id == tid,
                ChannelConnection.active.is_(True),
                ChannelConnection.deleted_at.is_(None),
                ES_MANUAL,
            )
            .order_by(ChannelConnection.is_primary.desc(), ChannelConnection.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def resolve_connection_credentials(
    type_id: str, tenant_id: uuid.UUID | None = None
) -> dict[str, Any] | None:
    """Credenciales (sin coercionar a str) de la conexión activa del tipo.

    Helper único de resolución por `(type_id, tenant)` (ADR-0069): lo usa un pack
    para leer su propia conexión (p. ej. los tokens de Google Calendar).
    """
    connection = await get_active_connection(type_id, tenant_id)
    if connection is None:
        return None
    return _decrypt_raw(connection)


async def resolve_credentials_for_external(external_id: str) -> dict[str, str] | None:
    """Credenciales de la conexión que matchea el phone_number_id del payload."""
    connection = await find_by_external_id(external_id)
    if connection is None or not connection.active:
        return None
    return decrypt_credentials(connection)


async def resolve_primary_credentials(tenant_id: uuid.UUID | None = None) -> dict[str, str] | None:
    """Credenciales de la conexión primaria activa, o None si no hay."""
    connection = await primary_connection(tenant_id)
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


async def resolve_effective_verify_token(tenant_id: uuid.UUID) -> str:
    """UN token que el handshake GET del webhook del tenant acepta (para el self-check del
    preflight). El handshake acepta el de cada conexión (`webhook_secrets.
    verify_tokens_aceptados`, HU-CONN-05); este devuelve uno de ellos.

    Orden: `ChannelConnection` (conexión manual) -> BYOK (`MetaConnection`,
    verify_token propio del wizard) -> `.env` (`WHATSAPP_VERIFY_TOKEN`).

    No usar `effective_credentials()` para decidir si hay token manual: su
    fallback a `env_credentials()` hace que `creds["verify_token"]` sea
    SIEMPRE truthy cuando el `.env` trae token global, dejando muerta la rama
    BYOK (bug real: el self-check del wizard BYOK respondía 403 porque el
    handler validaba con el token global).

    Para registrar la suscripción de UNA conexión se usa su propio token
    (`meta_health_service.verify_token_de_registro`), no este.
    El `tenant_id` va explícito a la resolución manual; el contexto
    (`tenant_scope`) sigue haciendo falta para RLS en la rama BYOK.
    """
    from core.gateway.meta_byok import resolve_webhook_verify_token

    manual = (await resolve_primary_credentials(tenant_id) or {}).get("verify_token")
    byok = await resolve_webhook_verify_token(tenant_id)
    return manual or byok or get_settings().whatsapp_verify_token or ""


def platform_app_secret() -> str:
    """App Secret de la app de la plataforma (`.env`): el único que puede autorizar
    eventos de cualquier negocio en el webhook (ADR-0100), porque ningún negocio lo
    conoce. Única fuente de este criterio. Los secretos propios se resuelven por
    conexión, en `core/gateway/webhook_secrets.py` (HU-CONN-05, ADR-0126)."""
    settings = get_settings()
    return settings.whatsapp_app_secret or settings.meta_app_secret or ""


def platform_app_secrets() -> tuple[str, ...]:
    """Todos los App Secrets de la plataforma con los que puede venir firmado un webhook: el de la app de Meta y,
    si hay, el de la app de Instagram (Instagram Login, HU-CHAN-04). Los dos son de la plataforma y ningún negocio
    los conoce, así que autorizan con la misma confianza (ADR-0100)."""
    settings = get_settings()
    return tuple(dict.fromkeys(s for s in (platform_app_secret(), getattr(settings, "instagram_app_secret", "")) if s))


async def save_connection(
    label: str,
    credentials: dict[str, Any],
    connection_id: uuid.UUID | None = None,
    type_id: str = WHATSAPP_META,
) -> tuple[ChannelConnection, bool]:
    """Crea o actualiza una conexión de un tipo del catálogo. Devuelve (conn, created).

    Las credenciales se validan/normalizan contra el `credentials_schema` del tipo
    (ADR-0069): se descartan claves no declaradas y se coercionan los tipos. La
    primera conexión de mensajería activa queda como primaria.
    """
    ctype = get_connection_type(type_id)
    if ctype is None:
        raise ValueError(f"Tipo de conexión desconocido: {type_id}")
    if type_id == WEBCHAT:
        # El chat web no es una conexión manual: lo crea Configuración → Chat web con su clave
        # generada por el servidor (`chat_web_conexion`, ADR-0115 §4).
        raise ValueError("El chat web se configura en Configuración → Chat web.")

    box = get_secret_box()
    tid = get_current_tenant_id()

    async with get_session() as session:
        previous: dict[str, Any] = {}
        connection: ChannelConnection | None = None
        if connection_id is not None:
            connection = await session.get(ChannelConnection, connection_id)
            if connection is None or administrada_aparte(connection):
                raise ValueError("La conexión no existe")
            if connection.tenant_id != tid:
                raise ValueError("La conexión no pertenece a este tenant")
            previous = _decrypt_raw(connection)

        clean = normalize_credentials(ctype, credentials, previous)
        external_id = external_id_for(ctype, clean)
        encrypted = box.encrypt_json(clean)

        if type_id == WHATSAPP_META and external_id:
            # El webhook rutea por este número: no puede ser de otro negocio (ADR-0095).
            from core.gateway.meta_asset_claims import MetaAssetError, claim_assets

            try:
                await claim_assets(session, tid, waba_id=None, phone_number_id=external_id)
            except MetaAssetError as exc:
                raise ValueError(exc.message) from exc

        if external_id and (
            await session.execute(
                select(ChannelConnection.id).where(
                    ChannelConnection.tenant_id == tid,
                    ChannelConnection.provider == ctype.provider,
                    ChannelConnection.external_id == external_id,
                    ChannelConnection.deleted_at.is_(None),
                    ChannelConnection.meta_connection_id.is_not(None),
                )
            )
        ).first():
            # El número ya tiene su conexión canónica de Meta (ADR-0115): una fila manual del
            # mismo número chocaría con ella (índice único y CHECK), al crear o al editar.
            raise ValueError(
                "Ese número ya está conectado con el asistente de WhatsApp: adminístralo desde esa conexión."
            )

        if connection is not None:
            connection.label = label
            connection.type_id = type_id
            connection.category = ctype.category
            connection.channel = ctype.channel
            connection.provider = ctype.provider
            connection.external_id = external_id or connection.external_id
            connection.credentials_encrypted = encrypted
            connection.active = True
            created = False
        else:
            # Lookup alineado al índice único `(tenant_id, provider, external_id)`
            # (defensa en profundidad + evita IntegrityError). Se prefiere la fila
            # activa sobre una soft-deleted para no chocar con el índice parcial.
            existing = (
                await session.execute(
                    select(ChannelConnection)
                    .where(
                        ChannelConnection.tenant_id == tid,
                        ChannelConnection.provider == ctype.provider,
                        ChannelConnection.external_id == external_id,
                    )
                    .order_by(
                        case((ChannelConnection.deleted_at.is_(None), 0), else_=1),
                        ChannelConnection.created_at.desc(),
                    )
                    .limit(1)
                )
            ).scalars().first()
            if existing is not None and administrada_aparte(existing):
                existing = None  # una canónica borrada no se convierte en manual
            if existing is not None:
                revivida = existing.deleted_at is not None
                existing.label = label
                existing.type_id = type_id
                existing.category = ctype.category
                existing.channel = ctype.channel
                existing.credentials_encrypted = encrypted
                existing.active = True
                existing.deleted_at = None  # re-agregar des-elimina (invariante #8)
                if revivida and ctype.produces_conversations:
                    await session.flush()
                    await _instancias_de_la_nueva(session, tid, existing.id)
                return existing, False
            connection = ChannelConnection(
                tenant_id=tid,
                channel=ctype.channel,
                provider=ctype.provider,
                type_id=type_id,
                category=ctype.category,
                label=label,
                active=True,
                is_primary=False,
                external_id=external_id,
                credentials_encrypted=encrypted,
            )
            session.add(connection)
            await session.flush()
            created = True

        # La primera conexión de MENSAJERÍA activa queda como primaria (ADR-0069)
        if created and ctype.category == CATEGORY_COMMUNICATION:
            count_active = (
                await session.execute(
                    select(func.count(ChannelConnection.id)).where(
                        ChannelConnection.active.is_(True),
                        ChannelConnection.tenant_id == tid,
                        ES_MANUAL,
                        or_(
                            ChannelConnection.category == CATEGORY_COMMUNICATION,
                            ChannelConnection.category.is_(None),
                        ),
                    )
                )
            ).scalar() or 0
            if count_active <= 1:
                connection.is_primary = True
        if created and ctype.produces_conversations:
            await _instancias_de_la_nueva(session, tid, connection.id)
        return connection, created


async def _instancias_de_la_nueva(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Coordinador y General de una conexión conversacional nueva o revivida, si el negocio ya
    tiene sus agentes (HU-AOS-13; en un SAVEPOINT: un error no tumba el alta)."""
    from core.agents.instancias import instancias_de_conexion_nueva

    await instancias_de_conexion_nueva(session, tenant_id, connection_id)


async def set_active(connection_id: uuid.UUID, active: bool) -> None:
    async with get_session() as session:
        connection = await session.get(ChannelConnection, connection_id)
        if connection is None or administrada_aparte(connection):
            return
        connection.active = active
        if not active:
            connection.is_primary = False


async def set_primary(connection_id: uuid.UUID) -> None:
    async with get_session() as session:
        target = await session.get(ChannelConnection, connection_id)
        if target is None or administrada_aparte(target):
            return
        # Solo se reasigna la primacía dentro de la misma categoría (ADR-0069) y
        # del mismo tenant; las filas legacy sin `category` cuentan como mensajería.
        rows = (
            await session.execute(
                select(ChannelConnection).where(
                    ChannelConnection.tenant_id == target.tenant_id,
                    ES_MANUAL,
                    or_(
                        ChannelConnection.category == target.category,
                        ChannelConnection.category.is_(None),
                    ),
                )
            )
        ).scalars().all()
        for row in rows:
            row.is_primary = row.id == connection_id
        target.active = True


async def delete_connection(connection_id: uuid.UUID) -> None:
    """Borra (soft-delete) una conexión — invariante #8: sin borrado físico."""
    async with get_session() as session:
        connection = await session.get(ChannelConnection, connection_id)
        if connection is not None and not administrada_aparte(connection):
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
            # `platform_type` y `account_mode`: si el número ya está registrado en la Cloud
            # API y si es de prueba de Meta o real (BYOK de producción, HU-CONN-06).
            query={"fields": "id,verified_name,display_phone_number,platform_type,account_mode,status"},
        )
    except GraphError as exc:
        text = f"{exc.message} {exc.error_user_msg or ''}".lower()
        expired = exc.code == 190 or "session has expired" in text or ("expired" in text and "token" in text)
        if expired:
            return (
                False,
                "El access token está vencido o es inválido. Generá uno nuevo y usá "
                "\"Extender a 60 días\" (o un token permanente de System User).",
                {},
            )
        return False, f"Meta rechazó las credenciales: {exc.message}", {}
    except Exception as exc:
        return False, f"Error de red con Meta: {str(exc)[:120]}", {}

    return True, "Credenciales verificadas con Meta", info or {}
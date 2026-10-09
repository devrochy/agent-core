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

`WEBHOOK_VERIFY_TOKEN_KIND`: el `verify_token` con el que se registra la
suscripción de la app y que el handshake acepta para esa conexión
(`webhook_secrets.verify_tokens_aceptados`, ADR-0126). El que escriba el admin
en el wizard o, si lo deja vacío, uno aleatorio propio de la conexión
(HU-CONN-06, ADR-0127): antes se registraba el global, que el handshake de
cualquier negocio acepta.

Entorno (HU-CONN-06, ADR-0127): el wizard elige `sandbox` (número de prueba de
Meta) o `production` (número real). En producción se rechaza un número de
prueba (`account_mode = SANDBOX`). En los dos, un número que Meta no reporta
registrado en la Cloud API (`platform_type` distinto de `CLOUD_API`) deja la
conexión `pending` con `MOTIVO_NUMERO_SIN_REGISTRAR` hasta que se registra con
su PIN (`meta_numbers_service.register_number`). Antes se marcaba registrado a
ciegas. Tampoco se acepta el App ID de la app de la plataforma: esa app la
comparten todos los negocios y se conecta con Embedded Signup (ADR-0126 §7).
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.db.models import MetaConnection, PhoneNumber, Tenant, WabaAccount, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.connections import verify_meta_credentials
from core.gateway.meta_asset_claims import (
    MetaAssetError,
    claim_assets,
    mover_conversaciones,
    numero_para_conexion,
    verify_waba_and_phone,
    waba_para_conexion,
)
from core.gateway.meta_connection_state import MOTIVO_NUMERO_SIN_REGISTRAR
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.shared.exceptions import AgentCoreError

APP_SECRET_KIND = "app_access"
CLOUD_API_ACCESS_TOKEN_KIND = "cloud_api_access_token"
WEBHOOK_VERIFY_TOKEN_KIND = "webhook_verify_token"


ENVIRONMENTS = ("sandbox", "production")

PLATFORM_APP_MESSAGE = (
    "Ese App ID es el de la app de Meta de la plataforma, que comparten todos los negocios: "
    "para usarla conecta con Facebook (Embedded Signup). Con BYOK va el App ID de tu propia app."
)
TEST_NUMBER_IN_PRODUCTION_MESSAGE = (
    "Ese número es el de prueba de Meta: elige «Prueba» o usa tu número real para producción."
)


class ByokError(AgentCoreError):
    """Credenciales BYOK rechazadas por Meta o incompletas."""


def numero_registrado(info: dict[str, Any]) -> bool:
    """¿Meta reporta el número registrado en la Cloud API? (`GET /{phone_number_id}`).

    `platform_type = CLOUD_API` es el registro; `NOT_APPLICABLE` (sin registrar) u
    `ON_PREMISE` piden registrarlo con el PIN. Sin `platform_type` se mira `status`, y sin
    ninguno de los dos (respuestas viejas) se asume registrado, como antes."""
    platform_type = info.get("platform_type")
    if platform_type is not None:
        return platform_type == "CLOUD_API"
    status = info.get("status")
    if status is not None:
        return status == "CONNECTED"
    return True


def es_numero_de_prueba(info: dict[str, Any]) -> bool:
    """¿Es un número de prueba de Meta? (`account_mode = SANDBOX`). Sin el campo, no."""
    return info.get("account_mode") == "SANDBOX"


def es_app_de_la_plataforma(app_id: str) -> bool:
    platform_app_id = get_settings().meta_app_id
    return bool(platform_app_id) and app_id == platform_app_id


def nuevo_verify_token() -> str:
    """`verify_token` propio de una conexión: aleatorio, nunca el global (ADR-0127)."""
    return secrets.token_urlsafe(32)


async def _token_expiry(app_id: str, app_secret: str, access_token: str) -> datetime | None:
    """Expiración del access token según `/debug_token`, o `None` si no expira
    (`expires_at == 0`) o si Meta no la reporta. Best-effort: nunca bloquea el
    alta de la conexión."""
    from core.gateway.meta_token_tools import inspect_token

    try:
        info = await inspect_token(app_id, app_secret, access_token)
    except (GraphError, httpx.HTTPError):
        # Un corte de red tampoco bloquea el alta (antes abortaba y la dejaba en `error`).
        return None
    expires_at = info.get("expires_at")
    if not expires_at:  # None o 0 (0 = "no expira" en Meta)
        return None
    return datetime.fromtimestamp(expires_at, UTC)


async def _subscribe_waba_to_app(
    token: str, waba_id: str, app_secret: str, version: str, connection_id: uuid.UUID, tenant_id: uuid.UUID,
) -> bool:
    """`POST /{waba_id}/subscribed_apps` -- sin esto la WABA nunca manda
    eventos de mensajes, aunque el webhook de la app esté bien configurado
    (mismo hallazgo/patrón que `meta_onboarding.py::_subscribe_waba_to_app`,
    reproducido en vivo: handshake OK, ninguna entrega de mensajes registrada
    porque faltaba este paso). Verificación inmediata (GET) -- un
    `{"success": true}` del POST no garantiza nada por sí solo."""
    await graph_fetch(
        f"/{waba_id}/subscribed_apps", method="POST", token=token, app_secret=app_secret,
        version=version, connection_id=connection_id, tenant_id=tenant_id,
    )
    verification = await graph_fetch(
        f"/{waba_id}/subscribed_apps", token=token, app_secret=app_secret,
        version=version, connection_id=connection_id, tenant_id=tenant_id,
    )
    return bool((verification or {}).get("data"))


async def save_byok_connection(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID | None,
    app_id: str,
    app_secret: str,
    phone_number_id: str,
    access_token: str,
    waba_id: str,
    environment: str = "sandbox",
    verify_token: str | None = None,
) -> MetaConnection:
    """Verifica las credenciales contra Graph API y recién después
    persiste -- nunca guarda a ciegas. Reentrante: volver a correrlo con
    el mismo `app_id` y entorno actualiza la conexión existente en vez de
    duplicar (respeta `uq_meta_connections_tenant_app`).

    Termina `connected`, o `pending` con `MOTIVO_NUMERO_SIN_REGISTRAR` si Meta no
    reporta el número registrado en la Cloud API (falta el PIN, HU-CONN-06)."""
    if not app_id or not app_secret:
        raise ByokError("Faltan App ID o App Secret")
    if not waba_id:
        raise ByokError("Falta el ID de la cuenta de WhatsApp Business (WABA)")
    if environment not in ENVIRONMENTS:
        raise ByokError("El entorno tiene que ser «sandbox» o «production»")
    if es_app_de_la_plataforma(app_id):
        raise ByokError(PLATFORM_APP_MESSAGE)
    verify_token = verify_token or nuevo_verify_token()

    ok, message, info = await verify_meta_credentials(phone_number_id, access_token, app_secret)
    if not ok:
        raise ByokError(message)
    if environment == "production" and es_numero_de_prueba(info):
        raise ByokError(TEST_NUMBER_IN_PRODUCTION_MESSAGE)
    registrado = numero_registrado(info)
    # El número existe para este token; falta que sea de ESA WABA y que el token la vea
    # (ADR-0095): el webhook rutea por la WABA cuando el evento no trae número.
    try:
        await verify_waba_and_phone(
            waba_id=waba_id, phone_number_id=phone_number_id, token=access_token,
            app_secret=app_secret, version=get_settings().graph_api_version, connection_id=None,
        )
    except MetaAssetError as exc:
        raise ByokError(exc.message) from exc

    async with get_session() as session:
        # Candado por activo + "¿lo tiene otro negocio?", en la misma transacción que
        # guarda las filas (ADR-0095).
        try:
            await claim_assets(session, tenant_id, waba_id=waba_id, phone_number_id=phone_number_id)
        except MetaAssetError as exc:
            raise ByokError(exc.message) from exc
        connection = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "whatsapp",
                    MetaConnection.environment == environment,
                    MetaConnection.meta_app_id == app_id,
                    # No reutilizar una conexión soft-deleted: la migración 0032
                    # hizo el unique PARCIAL (`where deleted_at is null`) justo
                    # para reconectar la misma app creando una fila NUEVA. Sin
                    # este filtro se resucitaba la vieja sin limpiar `deleted_at`
                    # -> quedaba con status "pending" pero borrada, invisible al
                    # resolver del webhook (403 en el self-check).
                    MetaConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        # Si el alta falla más abajo, `_rollback_failed_alta` deshace lo de esta transacción:
        # da de baja la fila que se creó aquí y le devuelve el número a su conexión anterior.
        created_new = connection is None
        previous_origin: str | None = None
        if connection is None:
            connection = MetaConnection(
                tenant_id=tenant_id, channel="whatsapp", environment=environment, meta_app_id=app_id,
                status="pending", origin="byok",
            )
            session.add(connection)
            await session.flush()
        else:
            # Estado intermedio hasta confirmar que los dos tokens se
            # guardaron -- recién al final se marca "connected" (ver abajo).
            connection.status = "pending"
            previous_origin = connection.origin
            connection.origin = "byok"  # guarda token y App Secret propios: ya es BYOK
        connection.status_reason = None
        connection.connected_by = admin_id
        connection.connected_at = utcnow()
        connection_id = connection.id

        # Una fila por número en el negocio (ADR-0122): al reconectar la misma app, o
        # después de eliminar la conexión vieja, se reutiliza (y revive) la fila y se
        # reasigna a esta conexión. Lo mismo la WABA, con sus plantillas y precios.
        # `phone_number_id` además es único entre las filas vivas de todos los negocios: si
        # otro lo tiene, el conflicto aparece en el flush y se traduce a un error de negocio
        # limpio (el wizard lo muestra) en vez de un IntegrityError crudo (500).
        waba = await waba_para_conexion(session, tenant_id, connection_id, waba_id)
        try:
            phone, previous_connection_id = await numero_para_conexion(
                session, tenant_id, connection_id, phone_number_id, waba_account_id=waba.id
            )
        except IntegrityError as exc:
            raise ByokError(
                f"El número de WhatsApp {phone_number_id} ya está asociado a otra conexión. "
                "Desconectalo desde el tenant que lo tiene, o usá otro número."
            ) from exc
        phone.display_phone_number = info.get("display_phone_number")
        phone.verified_name = info.get("verified_name")
        phone.platform_type = info.get("platform_type")
        phone.raw = {**(phone.raw or {}), **info}
        phone.registered = registrado

        # Ninguna WABA de la conexión cuenta como suscrita hasta que el `POST /subscribed_apps`
        # de abajo salga bien: al reconectar se reusa la conexión (y la fila, si es la misma
        # WABA), y un `True` de antes haría pasar por suscrito un reintento que falló
        # (`own_app_subscribed`, ADR-0102).
        await session.execute(
            update(WabaAccount)
            .where(WabaAccount.tenant_id == tenant_id, WabaAccount.connection_id == connection_id)
            .values(subscribed=False)
        )

    try:
        await meta_token_store.put_token(connection_id, APP_SECRET_KIND, app_id, app_secret, tenant_id=tenant_id)
        # Guardar la expiración del access token (si Meta la reporta) para poder
        # avisar en el panel antes de que venza -- best-effort, nunca bloquea el alta.
        token_expires_at = await _token_expiry(app_id, app_secret, access_token)
        await meta_token_store.put_token(
            connection_id, CLOUD_API_ACCESS_TOKEN_KIND, app_id, access_token,
            tenant_id=tenant_id, expires_at=token_expires_at,
        )
        await meta_token_store.put_token(
            connection_id, WEBHOOK_VERIFY_TOKEN_KIND, app_id, verify_token, tenant_id=tenant_id
        )

        settings = get_settings()
        subscribed = await _subscribe_waba_to_app(
            access_token, waba_id, app_secret, settings.graph_api_version, connection_id, tenant_id,
        )
        if not subscribed:
            raise ByokError(
                "Meta no confirmó la suscripción de la WABA (GET /subscribed_apps devolvió vacío) -- "
                "sin esto no llega ningún mensaje aunque el webhook esté bien configurado."
            )
        async with get_session() as session:
            db_waba = (
                await session.execute(
                    select(WabaAccount).where(
                        WabaAccount.tenant_id == tenant_id,
                        WabaAccount.connection_id == connection_id,
                        WabaAccount.waba_id == waba_id,
                    )
                )
            ).scalar_one_or_none()
            if db_waba is not None:
                db_waba.subscribed = True

        # Suscripción de CAMPOS a nivel de app ("messages", etc.) -- distinta
        # de la suscripción de OBJETO por WABA de arriba. Configurarla a mano
        # en el App Dashboard es frágil (el admin puede guardar el callback/
        # verify_token sin tildar el checkbox de "messages"): bug real
        # encontrado en vivo, el subscriptions GET mostraba `active=true` sin
        # ningún `fields`. Se hace por API para no depender de eso, mismo
        # mecanismo que usa Embedded Signup (`meta_onboarding.py`).
        from core.gateway.meta_preflight import AppCredentials
        from core.gateway.meta_subscriptions import SubscriptionError, upsert_app_subscription
        from core.tenants import tenant_webhook_url

        async with get_session() as session:
            tenant = await session.get(Tenant, tenant_id)
        if tenant is not None:
            config = AppCredentials(
                app_id=app_id, app_secret=app_secret, config_id="", graph_api_version=settings.graph_api_version,
                connection_id=connection_id,
            )
            try:
                await upsert_app_subscription(
                    config, "whatsapp_business_account",
                    callback_url=tenant_webhook_url(tenant.slug),
                    verify_token=verify_token,
                )
            except (SubscriptionError, GraphError) as exc:
                # `upsert_app_subscription` re-lanza el `GraphError` de Meta (p.ej.
                # `#2200 Callback verification failed`); sin capturarlo acá, escapaba
                # hasta la ruta del panel (que solo captura `ByokError`) -> 500.
                raise ByokError(f"No se pudo suscribir los campos de webhook en Meta: {exc}") from exc
    except BaseException as exc:
        # `BaseException`: una petición cancelada (`asyncio.CancelledError`) también deshace el
        # alta; si no, el intento quedaba `pending` con el número.
        # Las credenciales YA fueron verificadas contra Graph -- este
        # except cubre un fallo de infraestructura al guardar el token o al
        # suscribir la WABA (no credenciales inválidas), y evita dejar la
        # conexión en "connected" sin secretos reales o sin poder recibir
        # mensajes.
        detail = exc.message if isinstance(exc, GraphError) else (str(exc) or type(exc).__name__)
        # `shield`: si la petición se canceló, el rollback igual termina.
        await asyncio.shield(
            _rollback_failed_alta(
                tenant_id,
                connection_id,
                phone_number_id,
                created_new=created_new,
                previous_connection_id=previous_connection_id,
                previous_origin=previous_origin,
                reason=f"Fallo guardando credenciales o suscripción: {detail}"[:250],
            )
        )
        raise

    async with get_session() as session:
        db_connection = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.id == connection_id, MetaConnection.tenant_id == tenant_id
                )
            )
        ).scalar_one()
        if registrado:
            db_connection.status = "connected"
        else:
            # Falta el PIN: el registro (`register_number`) la deja `connected`.
            db_connection.status = "pending"
            db_connection.status_reason = MOTIVO_NUMERO_SIN_REGISTRAR
        return db_connection


async def _rollback_failed_alta(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    phone_number_id: str,
    *,
    created_new: bool,
    previous_connection_id: uuid.UUID | None,
    reason: str,
    previous_origin: str | None = None,
) -> None:
    """Deshace un alta BYOK que falló después de reasignar el número (BACKLOG, ADR-0101).

    Sin esto, la conexión del intento se quedaba con el número y, con un `connected_at` más
    nuevo, podía ganarle a la sana en `_resolve_byok_token` y dejar el inbound en 401.

    - La conexión del intento queda en `error` con el motivo (como antes).
    - El número vuelve a la conexión que lo tenía, si sigue viva y es de este negocio.
    - Si la fila la creó este intento, se da de baja (borrado lógico) y se liberan sus números
      y WABAs, como `soft_delete_connection`: deja de prestar su secreto y de contar como
      suscrita (`own_app_subscribed`).
    - Si se reusó (misma app), recupera su `origin` anterior: `_resolve_byok_token` ordena
      primero por origen y un intento fallido no la convierte en BYOK.
    """
    async with get_session() as session:
        connection = (
            await session.execute(
                select(MetaConnection).where(MetaConnection.id == connection_id, MetaConnection.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if connection is None:
            return
        connection.status = "error"
        connection.status_reason = reason
        if not created_new and previous_origin:
            connection.origin = previous_origin

        if previous_connection_id is not None and previous_connection_id != connection_id:
            previous = (
                await session.execute(
                    select(MetaConnection.id).where(
                        MetaConnection.id == previous_connection_id,
                        MetaConnection.tenant_id == tenant_id,
                        MetaConnection.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if previous is not None:
                await session.execute(
                    update(PhoneNumber)
                    .where(
                        PhoneNumber.tenant_id == tenant_id,
                        PhoneNumber.connection_id == connection_id,
                        PhoneNumber.phone_number_id == phone_number_id,
                    )
                    .values(connection_id=previous)
                )
                # Y sus conversaciones, si el intento se las había llevado.
                await mover_conversaciones(session, tenant_id, desde=connection_id, hacia=previous)

        if created_new:
            # Borrado lógico, como `soft_delete_connection` (invariante 8, ADR-0122): si el
            # intento revivió una WABA eliminada, sus plantillas y precios se conservan.
            now = utcnow()
            connection.deleted_at = now
            await session.execute(
                update(PhoneNumber)
                .where(
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.connection_id == connection_id,
                    PhoneNumber.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )
            await session.execute(
                update(WabaAccount)
                .where(
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.connection_id == connection_id,
                    WabaAccount.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )


# Estados en los que la conexión sigue siendo la credencial del negocio (ADR-0101).
# `degraded`/`error` los pone el cron de salud cuando falla un check (un token a menos de
# 7 días de vencer ya es `error`), o el alta si falla al guardar; Meta sigue firmando con
# el secreto de la app. `revoked` (pausada a mano) también, pero solo como último recurso
# (ADR-0122): su entrada se verifica y se guarda sin IA en vez de dar 401. Fuera quedan
# `draft` (sin credenciales) y las borradas (`deleted_at`). El webhook verifica cada grupo
# con el secreto de las conexiones en estos estados dueñas de sus activos (ADR-0126).
ESTADOS_CON_CREDENCIAL = ("pending", "connected", "degraded", "error", "revoked")


async def _resolve_byok_token(tenant_id: uuid.UUID, kind: str) -> str | None:
    """Token (del `kind` pedido) de UNA `MetaConnection` de WhatsApp del tenant: la que
    gana el orden de abajo. Hoy solo para el `verify_token` del self-check del preflight;
    la firma del webhook ya no sale de aquí sino de la conexión dueña de cada grupo
    (`webhook_secrets`, ADR-0126), y el handshake acepta el token de cada conexión.

    `None` si el tenant no tiene una conexión (o está a mitad de guardarse) o si
    no guardó ese token -- el llamador decide el fallback. Filtra `tenant_id`
    explícito, nunca confía solo en RLS (mismo criterio que el resto del módulo).

    Incluye `status="pending"` además de `"connected"` -- bug real encontrado en
    vivo: el self-check de `upsert_app_subscription` (llamado DESDE
    `save_byok_connection`, mientras la conexión todavía está `"pending"`) le pega
    a este mismo webhook para probarse a sí mismo, y si el filtro exigía
    `"connected"` no se encontraba a sí misma -- caía al `.env` global (vacío) y
    el self-check fallaba con 403.

    Incluye también `degraded` y `error` (ADR-0101): sin ellos, un check de salud
    fallido (un token por vencer, una plantilla rechazada, un sync atrasado) dejaba al
    negocio sin secreto y todo su inbound daba 401. Solo se usan como respaldo: primero
    `connected`/`pending`, después `degraded`, al final `error`.

    Primero las conexiones `origin="byok"`, cuyo modo vigente usa secreto propio
    (ADR-0094): una de Embedded Signup o manual más nueva no tiene esos tokens, y antes
    podía ganar el orden y dejar al negocio sin secreto. No se filtran: una conexión BYOK
    que tomó el wizard de Embedded Signup conserva su secreto, y su app puede seguir
    suscrita a la WABA (ADR-0101).

    Excluye `deleted_at` (invariante nº8): una conexión borrada no presta tokens.
    """
    async with get_session() as session:
        connection = (
            await session.execute(
                select(MetaConnection)
                .where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "whatsapp",
                    MetaConnection.status.in_(ESTADOS_CON_CREDENCIAL),
                    MetaConnection.deleted_at.is_(None),
                )
                .order_by(
                    # Una pausada nunca le gana a una viva, sea del origen que sea: una BYOK
                    # que tomó Embedded Signup conserva el secreto de su app (ADR-0122).
                    case((MetaConnection.status == "revoked", 1), else_=0),
                    case((MetaConnection.origin == "byok", 0), else_=1),
                    case((MetaConnection.status == "degraded", 1), (MetaConnection.status == "error", 2), else_=0),
                    MetaConnection.connected_at.desc(),
                )
            )
        ).scalars().first()
    if connection is None:
        return None
    return await meta_token_store.get_token(
        connection.id, kind, connection.meta_app_id, tenant_id=tenant_id
    )


async def resolve_webhook_verify_token(tenant_id: uuid.UUID) -> str | None:
    """`verify_token` propio del wizard BYOK del tenant, o `None` (el llamador
    cae a `settings.whatsapp_verify_token`)."""
    return await _resolve_byok_token(tenant_id, WEBHOOK_VERIFY_TOKEN_KIND)

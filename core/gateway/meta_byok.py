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
from datetime import UTC, datetime

import httpx
from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.db.models import MetaConnection, PhoneNumber, Tenant, WabaAccount, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.connections import verify_meta_credentials
from core.gateway.meta_asset_claims import MetaAssetError, claim_assets, verify_waba_and_phone
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.shared.exceptions import AgentCoreError

APP_SECRET_KIND = "app_access"
CLOUD_API_ACCESS_TOKEN_KIND = "cloud_api_access_token"
WEBHOOK_VERIFY_TOKEN_KIND = "webhook_verify_token"


class ByokError(AgentCoreError):
    """Credenciales BYOK rechazadas por Meta o incompletas."""


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
    el mismo `app_id` actualiza la conexión existente en vez de duplicar
    (respeta `uq_meta_connections_tenant_app`)."""
    if not app_id or not app_secret:
        raise ByokError("Faltan App ID o App Secret")
    if not waba_id:
        raise ByokError("Falta el ID de la cuenta de WhatsApp Business (WABA)")

    ok, message, info = await verify_meta_credentials(phone_number_id, access_token, app_secret)
    if not ok:
        raise ByokError(message)
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
            connection.origin = "byok"  # guarda token y App Secret propios: ya es BYOK
        connection.status_reason = None
        connection.connected_by = admin_id
        connection.connected_at = utcnow()
        connection_id = connection.id

        # `phone_number_id` es único GLOBAL (lo asigna Meta): al reconectar la
        # misma app el número ya existe apuntando a la conexión vieja, así que
        # hay que REUTILIZAR la fila y reasignarla a la conexión nueva. Filtrar
        # por `connection_id` haría un INSERT y violaría
        # `phone_numbers_phone_number_id_key` (bug real del flujo reconnect).
        # Filtro explícito de `tenant_id` (defensa en profundidad): un conexión
        # de otro tenant no puede reclamar el número de este.
        phone = (
            await session.execute(
                select(PhoneNumber).where(
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.phone_number_id == phone_number_id,
                )
            )
        ).scalar_one_or_none()
        previous_connection_id: uuid.UUID | None = None
        if phone is None:
            phone = PhoneNumber(tenant_id=tenant_id, connection_id=connection_id, phone_number_id=phone_number_id)
            session.add(phone)
        else:
            previous_connection_id = phone.connection_id
            phone.connection_id = connection_id
        phone.display_phone_number = info.get("display_phone_number")
        phone.verified_name = info.get("verified_name")
        phone.registered = True

        # `phone_number_id` es único GLOBAL y RLS impide ver la fila de OTRO
        # tenant: si el número ya lo reclamó otro tenant, el conflicto recién
        # aparece acá. Se traduce a un error de negocio limpio (el wizard y el
        # alta lo muestran como flash) en vez de un IntegrityError crudo (500).
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ByokError(
                f"El número de WhatsApp {phone_number_id} ya está asociado a otra conexión. "
                "Desconectalo desde el tenant que lo tiene, o usá otro número."
            ) from exc

        waba = (
            await session.execute(
                select(WabaAccount).where(
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.connection_id == connection_id,
                    WabaAccount.waba_id == waba_id,
                )
            )
        ).scalar_one_or_none()
        if waba is None:
            waba = WabaAccount(tenant_id=tenant_id, connection_id=connection_id, waba_id=waba_id)
            session.add(waba)
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
        if verify_token:
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
                    verify_token=verify_token or settings.whatsapp_verify_token,
                )
            except (SubscriptionError, GraphError) as exc:
                # `upsert_app_subscription` re-lanza el `GraphError` de Meta (p.ej.
                # `#2200 Callback verification failed`); sin capturarlo acá, escapaba
                # hasta la ruta del panel (que solo captura `ByokError`) -> 500.
                raise ByokError(f"No se pudo suscribir los campos de webhook en Meta: {exc}") from exc
    except Exception as exc:
        # Las credenciales YA fueron verificadas contra Graph -- este
        # except cubre un fallo de infraestructura al guardar el token o al
        # suscribir la WABA (no credenciales inválidas), y evita dejar la
        # conexión en "connected" sin secretos reales o sin poder recibir
        # mensajes.
        detail = exc.message if isinstance(exc, GraphError) else str(exc)
        await _rollback_failed_alta(
            tenant_id,
            connection_id,
            phone_number_id,
            created_new=created_new,
            previous_connection_id=previous_connection_id,
            reason=f"Fallo guardando credenciales o suscripción: {detail}"[:250],
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
        db_connection.status = "connected"
        return db_connection


async def _rollback_failed_alta(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    phone_number_id: str,
    *,
    created_new: bool,
    previous_connection_id: uuid.UUID | None,
    reason: str,
) -> None:
    """Deshace un alta BYOK que falló después de reasignar el número (BACKLOG, ADR-0101).

    Sin esto, la conexión del intento se quedaba con el número y, con un `connected_at` más
    nuevo, podía ganarle a la sana en `_resolve_byok_token` y dejar el inbound en 401.

    - La conexión del intento queda en `error` con el motivo (como antes).
    - El número vuelve a la conexión que lo tenía, si sigue viva y es de este negocio.
    - Si la fila la creó este intento, se da de baja (borrado lógico) y se liberan sus números
      y WABAs, como `soft_delete_connection`: deja de prestar su secreto y de contar como
      suscrita (`own_app_subscribed`).
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

        if created_new:
            connection.deleted_at = utcnow()
            await session.execute(
                delete(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.connection_id == connection_id)
            )
            await session.execute(
                delete(WabaAccount).where(WabaAccount.tenant_id == tenant_id, WabaAccount.connection_id == connection_id)
            )


# Estados en los que la conexión sigue siendo la credencial del negocio (ADR-0101).
# `degraded`/`error` los pone el cron de salud cuando falla un check (un token a menos de
# 7 días de vencer ya es `error`), o el alta si falla al guardar; Meta sigue firmando con
# el secreto de la app. Fuera quedan `revoked` (desactivada a mano o borrada: cortar es
# intencional) y `draft` (sin credenciales).
_ESTADOS_CON_CREDENCIAL = ("pending", "connected", "degraded", "error")


async def _resolve_byok_token(tenant_id: uuid.UUID, kind: str) -> str | None:
    """Token (del `kind` pedido) de la `MetaConnection` de WhatsApp del tenant.

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
                    MetaConnection.status.in_(_ESTADOS_CON_CREDENCIAL),
                    MetaConnection.deleted_at.is_(None),
                )
                .order_by(
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


async def resolve_byok_app_secret(tenant_id: uuid.UUID) -> str | None:
    """App Secret de la app de Meta del tenant (cargado por el wizard BYOK),
    o `None` (el llamador cae al `.env`). Se usa para validar la firma HMAC de
    los webhooks entrantes: la app dedicada del tenant firma con ESTE secreto,
    no con el global."""
    return await _resolve_byok_token(tenant_id, APP_SECRET_KIND)

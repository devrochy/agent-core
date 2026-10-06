"""Cron de salud de las suscripciones de webhook a nivel de app (cada 15
min): `GET /{app_id}/subscriptions` por cada app de Meta en uso, persiste el
estado y degrada las conexiones cuyo objeto dejó de estar activo.

Revisa también las apps de conexiones `degraded` y `error` (ADR-0102): son las que más
necesitan saber si la suscripción volvió, y el cron de salud solo deja mejorar una
conexión BYOK con la suscripción de su app confirmada.

Capacidad de CORE (no de un pack): se registra directo en el
`beat_schedule` de `core/tasks/celery_app.py`, sin pasar por
`register_cron` (que está atado a `plugin_manager.is_active`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from core.config import get_settings
from core.db.models import MetaConnection
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_subscriptions import (
    OBJECT_TO_CHANNEL,
    SubscriptionError,
    fetch_app_access_token,
    humanize_subscription_error,
    persist_subscription_state,
)
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


def _normalize_fields(raw: object) -> list[str]:
    """Normaliza los `fields` que devuelve Meta a `list[str]`.

    La Graph API respondía `["messages", ...]`; desde v26 devuelve objetos
    `[{"name": "messages", "version": "v26.0"}, ...]`. Al insertarlos en una
    columna `text[]` eso reventaba con `DataError: invalid input for query
    argument` y el cron de salud no podía persistir el estado.
    """
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                result.append(str(name))
        elif item:
            result.append(str(item))
    return result



@dataclass
class _AppGroup:
    meta_app_id: str
    app_secret: str = field(repr=False)
    graph_api_version: str
    representative_connection_id: uuid.UUID
    representative_tenant_id: uuid.UUID
    connections: list[MetaConnection]
    # `(connection_id, tenant_id)` de otras conexiones de la app con un secreto DISTINTO, en
    # orden: si la representativa falla (p. ej. un negocio cargó el `app_id` de otro con su
    # propio secreto), se prueba con ellas antes de dar la app por caída (ADR-0102). Sin el
    # secreto: se lee recién al usarlo, bajo el scope de su dueño.
    respaldos: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)


# Estados cuyas apps se revisan (ADR-0102). No solo `connected`: este cron degrada las
# conexiones de una app con la suscripción caída, y si después no la revisaba más, el
# `active=False` quedaba viejo y la conexión no se recuperaba nunca.
_ESTADOS_A_REVISAR = ("connected", "degraded", "error")
_ORDEN_REPRESENTATIVA = {status: i for i, status in enumerate(_ESTADOS_A_REVISAR)}


async def _group_connected_apps() -> list[_AppGroup]:
    """Una fila por `meta_app_id` distinto con al menos una `MetaConnection`
    `connected`/`degraded`/`error`, guardando TODAS sus conexiones
    (necesarias para poder degradar cada una bajo su propio `tenant_scope` más abajo --
    una misma app puede estar en uso por tenants distintos, BYOK real).

    Enumera ids cross-tenant vía `admin_meta_connection_ids` (SECURITY DEFINER,
    migración 0033) y luego hidrata cada fila bajo el `tenant_scope` de su
    tenant -- un `select` plano sin scope devolvería 0 filas por RLS."""
    from core.db.admin_enum import admin_meta_connection_ids

    all_connections: list[MetaConnection] = []
    for connection_id, tenant_id in await admin_meta_connection_ids(list(_ESTADOS_A_REVISAR)):
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                connection = await session.get(MetaConnection, connection_id)
                # El estado se vuelve a mirar: pudo cambiar desde que se enumeró (una recién
                # desactivada no debe representar a la app).
                if (
                    connection is not None
                    and connection.tenant_id == tenant_id
                    and connection.status in _ESTADOS_A_REVISAR
                    and connection.deleted_at is None
                ):
                    all_connections.append(connection)

    by_app: dict[str, list[MetaConnection]] = {}
    for row in all_connections:
        by_app.setdefault(row.meta_app_id, []).append(row)

    platform_app_id = get_settings().meta_app_id
    groups: list[_AppGroup] = []
    for meta_app_id, connections in by_app.items():
        if platform_app_id and meta_app_id == platform_app_id:
            # La app de la plataforma no se revisa desde acá: sus conexiones (Embedded Signup,
            # manual) no guardan su secreto, y uno que diga ser de ella es un alta BYOK con un
            # `app_id` ajeno. Usarlo pisaría cada 15 min el estado que ven todos los negocios
            # de Embedded Signup (auditor-multitenant, ADR-0102).
            continue
        # Orden determinístico (la query no trae ORDER BY), estable entre corridas del
        # cron -- hallazgo auditor-multitenant, 2026-09-21 (severidad baja). Primero las
        # sanas: la representativa es la primera CON App Secret (ADR-0102). Antes era la
        # más antigua y, si no tenía secreto, se saltaba toda la app.
        connections = sorted(connections, key=lambda c: (_ORDEN_REPRESENTATIVA[c.status], c.created_at))
        secrets = [await _app_secret_de(connection.id, connection.tenant_id, meta_app_id) for connection in connections]
        con_secreto = [(c, secret) for c, secret in zip(connections, secrets, strict=True) if secret]
        if not con_secreto:
            log.warning("meta_subscriptions_health_sin_app_secret", meta_app_id=meta_app_id)
            continue
        representative, app_secret = con_secreto[0]

        # Una misma app compartida por BYOK debería tener el MISMO app_secret
        # guardado por cada tenant que la usa -- si divergen, operar con el
        # de la conexión representativa estaría actuando "en nombre de" otro
        # tenant con credenciales que no son las suyas. No es una fuga de
        # datos (el secreto nunca se expone), pero sí una inconsistencia que
        # merece quedar loggeada en vez de pasar en silencio (hallazgo
        # auditor-multitenant, 2026-09-21, severidad media).
        for other, other_secret in zip(connections, secrets, strict=True):
            if other is representative:
                continue
            if other_secret and other_secret != app_secret:
                log.warning(
                    "meta_subscriptions_health_app_secret_divergente",
                    meta_app_id=meta_app_id,
                    representative_tenant_id=str(representative.tenant_id),
                    otro_tenant_id=str(other.tenant_id),
                )
        respaldos: list[tuple[uuid.UUID, uuid.UUID]] = []
        probados = {app_secret}
        for other, other_secret in con_secreto[1:]:
            if other_secret not in probados:
                probados.add(other_secret)
                respaldos.append((other.id, other.tenant_id))
        groups.append(
            _AppGroup(
                meta_app_id=meta_app_id,
                app_secret=app_secret,
                graph_api_version=representative.graph_api_version,
                representative_connection_id=representative.id,
                representative_tenant_id=representative.tenant_id,
                connections=connections,
                respaldos=respaldos,
            )
        )
    return groups


async def _app_secret_de(connection_id: uuid.UUID, tenant_id: uuid.UUID, meta_app_id: str) -> str | None:
    """App Secret de la conexión, bajo el `tenant_scope` de SU negocio. Una fila que no se
    puede leer (un secreto que no descifra) se loguea y cuenta como sin secreto: no tumba el
    cron de todos."""
    try:
        async with tenant_scope(tenant_id):
            return await meta_token_store.get_token(connection_id, "app_access", meta_app_id, tenant_id=tenant_id)
    except Exception:
        log.exception(
            "meta_subscriptions_health_secreto_ilegible",
            meta_app_id=meta_app_id, connection_id=str(connection_id), tenant_id=str(tenant_id),
        )
        return None


async def _degrade_connections_for_object(connections: list[MetaConnection], object_: str) -> None:
    """Marca `status='degraded'` solo las conexiones `connected` cuyo canal
    corresponde al objeto caído -- nunca pisa `revoked`/`error`/`draft`, y
    actualiza cada conexión bajo el `tenant_scope` de SU PROPIO tenant (una
    app puede tener conexiones de tenants distintos; RLS de `meta_connections`
    exige el contexto correcto para el UPDATE de cada una)."""
    channel = OBJECT_TO_CHANNEL.get(object_)
    if channel is None:
        return
    for connection in connections:
        if connection.channel != channel or connection.status != "connected":
            continue
        async with tenant_scope(connection.tenant_id):
            async with get_session() as session:
                # FOR UPDATE: un "Desactivar" concurrente no queda pisado por `degraded`, que
                # volvería a prestar el secreto (ADR-0101).
                db_connection = await session.get(MetaConnection, connection.id, with_for_update=True)
                if (
                    db_connection is None
                    or db_connection.tenant_id != connection.tenant_id
                    or db_connection.status != "connected"
                ):
                    continue  # cambió mientras tanto: no se degradó nada
                db_connection.status = "degraded"
                db_connection.status_reason = f"meta_subscription_inactive:{object_}"
                await session.commit()
        log.error(
            "meta_subscription_degraded",
            meta_app_id=connection.meta_app_id,
            object=object_,
            connection_id=str(connection.id),
            tenant_id=str(connection.tenant_id),
        )


async def _fetch_subscriptions(group: _AppGroup, connection_id: uuid.UUID, tenant_id: uuid.UUID, app_secret: str):
    async with tenant_scope(tenant_id):
        token = await fetch_app_access_token(group.meta_app_id, app_secret, group.graph_api_version)
        return await graph_fetch(
            f"/{group.meta_app_id}/subscriptions",
            token=token,
            app_secret=app_secret,
            version=group.graph_api_version,
            connection_id=connection_id,
        )


async def _check_group(group: _AppGroup) -> None:
    candidatas = [(group.representative_connection_id, group.representative_tenant_id), *group.respaldos]
    data = None
    primer_error: GraphError | SubscriptionError | None = None
    for connection_id, tenant_id in candidatas:
        if connection_id == group.representative_connection_id:
            app_secret = group.app_secret
        else:
            app_secret = await _app_secret_de(connection_id, tenant_id, group.meta_app_id)
            if not app_secret:
                continue
        try:
            data = await _fetch_subscriptions(group, connection_id, tenant_id, app_secret)
            break
        except (GraphError, SubscriptionError) as exc:
            # Se guarda el error de la representativa: el de un respaldo con un secreto ajeno
            # ("App Secret incorrecto") no describe a la app.
            primer_error = primer_error or exc
            log.error(
                "meta_subscriptions_health_check_fallo",
                meta_app_id=group.meta_app_id,
                connection_id=str(connection_id),
                detail=humanize_subscription_error(exc) if isinstance(exc, GraphError) else str(exc),
            )
    else:
        # Fallaron todas: recién ahí la app queda "sin dato". Un `SubscriptionError` (p. ej.
        # un timeout al pedir el token de la app) sigue subiendo como antes, sin pisar el estado.
        if isinstance(primer_error, SubscriptionError):
            raise primer_error
        detail = humanize_subscription_error(primer_error) if primer_error is not None else "Sin secreto"
        for object_ in OBJECT_TO_CHANNEL:
            await persist_subscription_state(
                group.meta_app_id, object_, callback_url=None, active=None, fields=[], last_error=detail,
            )
        return

    items = (data or {}).get("data", [])
    for item in items:
        object_ = item.get("object", "")
        active = item.get("active", False)
        fields = _normalize_fields(item.get("fields", []))
        callback_url = item.get("callback_url")
        await persist_subscription_state(
            group.meta_app_id, object_, callback_url=callback_url, active=active, fields=fields, last_error=None
        )
        if active is False:
            await _degrade_connections_for_object(group.connections, object_)


async def check_all_subscriptions_health() -> None:
    """Nunca levanta excepción: una app caída no debe interrumpir el chequeo
    de las demás (mismo criterio que `meta_preflight.run_preflight`)."""
    groups = await _group_connected_apps()
    for group in groups:
        try:
            await _check_group(group)
        except Exception:
            log.exception("meta_subscriptions_health_check_error_inesperado", meta_app_id=group.meta_app_id)

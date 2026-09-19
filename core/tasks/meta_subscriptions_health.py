"""Cron de salud de las suscripciones de webhook a nivel de app (cada 15
min): `GET /{app_id}/subscriptions` por cada app de Meta en uso, persiste el
estado y degrada las conexiones cuyo objeto dejó de estar activo.

Capacidad de CORE (no de un pack): se registra directo en el
`beat_schedule` de `core/tasks/celery_app.py`, sin pasar por
`register_cron` (que está atado a `plugin_manager.is_active`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.db.models import MetaConnection
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_subscriptions import (
    OBJECT_TO_CHANNEL,
    fetch_app_access_token,
    humanize_subscription_error,
    persist_subscription_state,
)
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


@dataclass
class _AppGroup:
    meta_app_id: str
    app_secret: str
    graph_api_version: str
    representative_connection_id: uuid.UUID
    representative_tenant_id: uuid.UUID
    connections: list[MetaConnection]


async def _group_connected_apps() -> list[_AppGroup]:
    """Una fila por `meta_app_id` distinto con al menos una `MetaConnection`
    `connected`, guardando TODAS sus conexiones (necesarias para poder
    degradar cada una bajo su propio `tenant_scope` más abajo -- una misma
    app puede estar en uso por tenants distintos, BYOK real)."""
    async with get_session() as session:
        rows = (
            await session.execute(select(MetaConnection).where(MetaConnection.status == "connected"))
        ).scalars().all()

    by_app: dict[str, list[MetaConnection]] = {}
    for row in rows:
        by_app.setdefault(row.meta_app_id, []).append(row)

    groups: list[_AppGroup] = []
    for meta_app_id, connections in by_app.items():
        # Orden determinístico (la query no trae ORDER BY): la conexión más
        # antigua es la "representativa" de forma estable entre corridas del
        # cron -- hallazgo auditor-multitenant, 2026-09-21 (severidad baja).
        connections = sorted(connections, key=lambda c: c.created_at)
        representative = connections[0]
        async with tenant_scope(representative.tenant_id):
            app_secret = await meta_token_store.get_token(
                representative.id, "app_access", meta_app_id, tenant_id=representative.tenant_id
            )
        if not app_secret:
            log.warning("meta_subscriptions_health_sin_app_secret", meta_app_id=meta_app_id)
            continue

        # Una misma app compartida por BYOK debería tener el MISMO app_secret
        # guardado por cada tenant que la usa -- si divergen, operar con el
        # de la conexión representativa estaría actuando "en nombre de" otro
        # tenant con credenciales que no son las suyas. No es una fuga de
        # datos (el secreto nunca se expone), pero sí una inconsistencia que
        # merece quedar loggeada en vez de pasar en silencio (hallazgo
        # auditor-multitenant, 2026-09-21, severidad media).
        for other in connections[1:]:
            async with tenant_scope(other.tenant_id):
                other_secret = await meta_token_store.get_token(
                    other.id, "app_access", meta_app_id, tenant_id=other.tenant_id
                )
            if other_secret and other_secret != app_secret:
                log.warning(
                    "meta_subscriptions_health_app_secret_divergente",
                    meta_app_id=meta_app_id,
                    representative_tenant_id=str(representative.tenant_id),
                    otro_tenant_id=str(other.tenant_id),
                )
        groups.append(
            _AppGroup(
                meta_app_id=meta_app_id,
                app_secret=app_secret,
                graph_api_version=representative.graph_api_version,
                representative_connection_id=representative.id,
                representative_tenant_id=representative.tenant_id,
                connections=connections,
            )
        )
    return groups


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
                db_connection = await session.get(MetaConnection, connection.id)
                if db_connection is not None and db_connection.status == "connected":
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


async def _check_group(group: _AppGroup) -> None:
    try:
        async with tenant_scope(group.representative_tenant_id):
            token = await fetch_app_access_token(group.meta_app_id, group.app_secret, group.graph_api_version)
            data = await graph_fetch(
                f"/{group.meta_app_id}/subscriptions",
                token=token,
                app_secret=group.app_secret,
                version=group.graph_api_version,
                connection_id=group.representative_connection_id,
            )
    except GraphError as exc:
        log.error(
            "meta_subscriptions_health_check_fallo",
            meta_app_id=group.meta_app_id,
            detail=humanize_subscription_error(exc),
        )
        for object_ in OBJECT_TO_CHANNEL:
            await persist_subscription_state(
                group.meta_app_id, object_, callback_url=None, active=None, fields=[],
                last_error=humanize_subscription_error(exc),
            )
        return

    items = (data or {}).get("data", [])
    for item in items:
        object_ = item.get("object", "")
        active = item.get("active", False)
        fields = item.get("fields", [])
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
